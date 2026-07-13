"""Durable, provider-neutral Shuttle launch records.

Each launcher invocation owns one immutable ``launch_id`` record. Provider
hooks join that record through ``SHUTTLE_LAUNCH_ID`` and add the provider's
native session identity and current state. Records are deliberately small JSON
documents so shell launchers and future providers can use them without a
database migration.
"""

from __future__ import annotations

import copy
import fcntl
import json
import os
import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

SCHEMA_VERSION = 1
STATES = frozenset({"starting", "working", "approval", "idle", "closed"})
LIVE_STATES = STATES - {"closed"}
CLOSE_STATUSES = frozenset({"exited", "killed", "failed", "superseded", "unknown"})

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_REQUIRED_FIELDS = frozenset(
    {
        "schema_version",
        "launch_id",
        "provider",
        "provider_version",
        "mode",
        "tmux_session",
        "pane_id",
        "pid",
        "cwd",
        "brief",
        "title",
        "native_session_id",
        "resume_of",
        "state",
        "created_at",
        "updated_at",
        "state_changed_at",
        "closed_at",
        "close_status",
        "exit_code",
        "degraded",
        "errors",
    }
)
_TRANSITIONS = {
    "starting": frozenset({"starting", "working", "approval", "idle", "closed"}),
    "working": frozenset({"starting", "working", "approval", "idle", "closed"}),
    "approval": frozenset({"starting", "approval", "working", "idle", "closed"}),
    "idle": frozenset({"starting", "idle", "working", "approval", "closed"}),
    "closed": frozenset({"closed"}),
}


class RegistryError(Exception):
    """Base class for registry failures callers may handle."""


class InvalidRecord(RegistryError):
    """A record or requested field is invalid."""


class CorruptRecord(InvalidRecord):
    """An on-disk record cannot be parsed or fails schema validation."""


class LaunchNotFound(RegistryError):
    """No launch record exists for a launch id."""


class BindingConflict(RegistryError):
    """A launch is already bound to a different native session id."""


class ResumeIdentityConflict(BindingConflict):
    """A resume launch started a different native session than requested."""


class InvalidTransition(RegistryError):
    """A state change is not permitted by the launch state machine."""


class ClosedLaunch(InvalidTransition):
    """A caller tried to mutate a terminal launch record."""


@dataclass(frozen=True)
class ScanIssue:
    path: Path
    error: str


@dataclass(frozen=True)
class ScanResult:
    records: tuple[dict[str, Any], ...]
    issues: tuple[ScanIssue, ...]


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _optional_string(name: str, value: Any) -> None:
    if value is not None and not isinstance(value, str):
        raise InvalidRecord(f"{name} must be a string or null")


def _validate_id(name: str, value: str) -> None:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise InvalidRecord(f"invalid {name}: {value!r}")


def validate_record(record: Mapping[str, Any]) -> None:
    """Validate schema v1 fields needed for safe registry operations."""

    missing = sorted(_REQUIRED_FIELDS - record.keys())
    if missing:
        raise InvalidRecord(f"missing fields: {', '.join(missing)}")
    if record["schema_version"] != SCHEMA_VERSION:
        raise InvalidRecord(f"unsupported schema_version: {record['schema_version']!r}")
    _validate_id("launch_id", record["launch_id"])
    for field in ("provider", "provider_version", "mode", "cwd"):
        if not isinstance(record[field], str) or not record[field]:
            raise InvalidRecord(f"{field} must be a non-empty string")
    for field in (
        "tmux_session",
        "pane_id",
        "brief",
        "title",
        "native_session_id",
        "resume_of",
        "closed_at",
        "close_status",
    ):
        _optional_string(field, record[field])
    for field in ("native_session_id", "resume_of"):
        if record[field] == "":
            raise InvalidRecord(f"{field} cannot be empty")
    if (
        record["native_session_id"] is not None
        and record["resume_of"] is not None
        and record["native_session_id"] != record["resume_of"]
    ):
        raise InvalidRecord("native_session_id must equal resume_of when both are set")
    if record["pid"] is not None and (
        not isinstance(record["pid"], int)
        or isinstance(record["pid"], bool)
        or record["pid"] <= 0
    ):
        raise InvalidRecord("pid must be a positive integer or null")
    if record["exit_code"] is not None and (
        not isinstance(record["exit_code"], int)
        or isinstance(record["exit_code"], bool)
    ):
        raise InvalidRecord("exit_code must be an integer or null")
    if not isinstance(record["state"], str) or record["state"] not in STATES:
        raise InvalidRecord(f"invalid state: {record['state']!r}")
    for field in ("created_at", "updated_at", "state_changed_at"):
        if not isinstance(record[field], str) or not record[field]:
            raise InvalidRecord(f"{field} must be a non-empty string")
    if not isinstance(record["degraded"], bool):
        raise InvalidRecord("degraded must be boolean")
    if not isinstance(record["errors"], list):
        raise InvalidRecord("errors must be a list")
    if record["state"] == "closed":
        if record["closed_at"] is None or record["close_status"] is None:
            raise InvalidRecord("closed records require closed_at and close_status")
        if record["close_status"] not in CLOSE_STATUSES:
            raise InvalidRecord(f"invalid close status: {record['close_status']!r}")
    elif any(
        record[field] is not None
        for field in ("closed_at", "close_status", "exit_code")
    ):
        raise InvalidRecord("live records cannot contain closure fields")


class Registry:
    """Filesystem-backed launch registry with process-safe atomic updates."""

    def __init__(
        self,
        home: str | os.PathLike[str] | None = None,
        *,
        clock: Callable[[], str] = _utc_now,
    ) -> None:
        configured_home = home
        if configured_home is None:
            configured_home = os.environ.get("SHUTTLE_HOME")
        self.home = (
            Path(configured_home).expanduser()
            if configured_home
            else Path.home() / ".shuttle"
        )
        self.sessions_dir = self.home / "sessions"
        self.failures_dir = self.sessions_dir / "failures"
        self._lock_path = self.sessions_dir / ".registry.lock"
        self._clock = clock

    def _ensure_dirs(self) -> None:
        self.sessions_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            self.sessions_dir.chmod(0o700)
        except OSError:
            pass

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self._ensure_dirs()
        fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _path(self, launch_id: str) -> Path:
        _validate_id("launch_id", launch_id)
        return self.sessions_dir / f"{launch_id}.json"

    def _read_unlocked(self, launch_id: str) -> dict[str, Any]:
        path = self._path(launch_id)
        try:
            raw = path.read_bytes()
        except FileNotFoundError as exc:
            raise LaunchNotFound(launch_id) from exc
        try:
            record = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CorruptRecord(f"{path}: invalid JSON: {exc}") from exc
        if not isinstance(record, dict):
            raise CorruptRecord(f"{path}: record must be a JSON object")
        try:
            validate_record(record)
        except InvalidRecord as exc:
            raise CorruptRecord(f"{path}: {exc}") from exc
        if record["launch_id"] != launch_id:
            raise CorruptRecord(
                f"{path}: launch_id is {record['launch_id']!r}, expected {launch_id!r}"
            )
        return record

    def _fsync_directory(self, directory: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        fd = os.open(directory, flags)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _write_unlocked(self, path: Path, document: Mapping[str, Any]) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
        payload = (
            json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path)
            self._fsync_directory(path.parent)
        finally:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass

    def create_launch(
        self,
        *,
        provider: str,
        mode: str,
        cwd: str | os.PathLike[str],
        launch_id: str | None = None,
        tmux_session: str | None = None,
        pane_id: str | None = None,
        pid: int | None = None,
        brief: str | None = None,
        title: str | None = None,
        native_session_id: str | None = None,
        resume_of: str | None = None,
        provider_version: str | None = None,
    ) -> dict[str, Any]:
        """Create a launch record without overwriting an existing launch id."""

        launch_id = launch_id or str(uuid.uuid4())
        _validate_id("launch_id", launch_id)
        if native_session_id is not None and resume_of not in (None, native_session_id):
            raise ResumeIdentityConflict(
                f"resume_of {resume_of!r} differs from native session {native_session_id!r}"
            )
        now = self._clock()
        record: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "launch_id": launch_id,
            "provider": provider,
            "provider_version": provider_version or "unknown",
            "mode": mode,
            "tmux_session": tmux_session,
            "pane_id": pane_id,
            "pid": pid,
            "cwd": os.fspath(cwd),
            "brief": brief,
            "title": title,
            "native_session_id": native_session_id,
            "resume_of": resume_of,
            "state": "starting",
            "created_at": now,
            "updated_at": now,
            "state_changed_at": now,
            "closed_at": None,
            "close_status": None,
            "exit_code": None,
            "degraded": False,
            "errors": [],
        }
        validate_record(record)
        path = self._path(launch_id)
        with self._locked():
            if path.exists():
                raise InvalidRecord(f"launch already exists: {launch_id}")
            self._write_unlocked(path, record)
        return copy.deepcopy(record)

    def get(self, launch_id: str) -> dict[str, Any]:
        with self._locked():
            return copy.deepcopy(self._read_unlocked(launch_id))

    def scan(self) -> ScanResult:
        """Read valid records and report corrupt/partial records without failing."""

        records: list[dict[str, Any]] = []
        issues: list[ScanIssue] = []
        with self._locked():
            for path in sorted(self.sessions_dir.glob("*.json")):
                if path.name.startswith("."):
                    continue
                try:
                    records.append(self._read_unlocked(path.stem))
                except RegistryError as exc:
                    issues.append(ScanIssue(path=path, error=str(exc)))
        records.sort(key=lambda item: (item["created_at"], item["launch_id"]))
        return ScanResult(records=tuple(copy.deepcopy(records)), issues=tuple(issues))

    def list_launches(
        self, *, include_closed: bool = True, collapse_native: bool = False
    ) -> list[dict[str, Any]]:
        records = list(self.scan().records)
        if not include_closed:
            records = [record for record in records if record["state"] != "closed"]
        if collapse_native:
            records = self._collapse_native(records)
        return records

    @staticmethod
    def _collapse_native(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        newest: dict[str, dict[str, Any]] = {}
        unbound: list[dict[str, Any]] = []
        closed: list[dict[str, Any]] = []
        for record in records:
            native_id = record["native_session_id"]
            if record["state"] == "closed":
                closed.append(record)
            elif native_id is None:
                unbound.append(record)
            else:
                previous = newest.get(native_id)
                if previous is None or (record["created_at"], record["launch_id"]) > (
                    previous["created_at"],
                    previous["launch_id"],
                ):
                    newest[native_id] = record
        result = closed + unbound + list(newest.values())
        result.sort(key=lambda item: (item["created_at"], item["launch_id"]))
        return result

    def latest_live_for_native(self, native_session_id: str) -> dict[str, Any] | None:
        matches = [
            record
            for record in self.list_launches(include_closed=False)
            if record["native_session_id"] == native_session_id
        ]
        if not matches:
            return None
        return copy.deepcopy(
            max(matches, key=lambda item: (item["created_at"], item["launch_id"]))
        )

    def _mutate(
        self,
        launch_id: str,
        mutator: Callable[[dict[str, Any]], None],
        *,
        allow_closed: bool = False,
    ) -> dict[str, Any]:
        path = self._path(launch_id)
        with self._locked():
            record = self._read_unlocked(launch_id)
            if record["state"] == "closed" and not allow_closed:
                raise ClosedLaunch(f"launch is closed: {launch_id}")
            mutator(record)
            validate_record(record)
            self._write_unlocked(path, record)
            return copy.deepcopy(record)

    def bind_native(
        self,
        launch_id: str,
        native_session_id: str,
        *,
        resumed: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(native_session_id, str) or not native_session_id:
            raise InvalidRecord("native_session_id must be a non-empty string")

        def bind(record: dict[str, Any]) -> None:
            current = record["native_session_id"]
            resume_of = record["resume_of"]
            if resume_of is not None and resume_of != native_session_id:
                raise ResumeIdentityConflict(
                    f"launch {launch_id} resumes {resume_of!r}, not {native_session_id!r}"
                )
            if current is not None and current != native_session_id:
                raise BindingConflict(
                    f"launch {launch_id} is bound to {current!r}, not {native_session_id!r}"
                )
            changed = current is None
            if changed:
                record["native_session_id"] = native_session_id
            if resumed and resume_of is None:
                record["resume_of"] = native_session_id
                changed = True
            if changed:
                record["updated_at"] = self._clock()

        return self._mutate(launch_id, bind)

    def bind_location(
        self,
        launch_id: str,
        *,
        tmux_session: str,
        pane_id: str,
        pid: int,
    ) -> dict[str, Any]:
        """Bind the exact live tmux/process location for a supervised launch.

        Location is launcher-owned rather than provider-owned.  Repeating the
        same binding is harmless, but changing any non-null value would make
        targeting unsafe and is therefore rejected.
        """

        if not isinstance(tmux_session, str) or not tmux_session:
            raise InvalidRecord("tmux_session must be a non-empty string")
        if not isinstance(pane_id, str) or not pane_id:
            raise InvalidRecord("pane_id must be a non-empty string")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            raise InvalidRecord("pid must be a positive integer")

        def bind(record: dict[str, Any]) -> None:
            requested = {
                "tmux_session": tmux_session,
                "pane_id": pane_id,
                "pid": pid,
            }
            for field, value in requested.items():
                current = record[field]
                if current is not None and current != value:
                    raise BindingConflict(
                        f"launch {launch_id} {field} is {current!r}, not {value!r}"
                    )
            changed = any(record[field] is None for field in requested)
            if changed:
                record.update(requested)
                record["updated_at"] = self._clock()

        return self._mutate(launch_id, bind)

    def transition(self, launch_id: str, state: str) -> dict[str, Any]:
        if state not in LIVE_STATES:
            if state == "closed":
                raise InvalidTransition("use close() to enter the closed state")
            raise InvalidTransition(f"unknown state: {state!r}")

        def change(record: dict[str, Any]) -> None:
            current = record["state"]
            if state not in _TRANSITIONS[current]:
                raise InvalidTransition(f"cannot transition {current} -> {state}")
            if current != state:
                now = self._clock()
                record["state"] = state
                record["state_changed_at"] = now
                record["updated_at"] = now

        return self._mutate(launch_id, change)

    def close(
        self,
        launch_id: str,
        *,
        status: str,
        exit_code: int | None = None,
    ) -> dict[str, Any]:
        if status not in CLOSE_STATUSES:
            raise InvalidRecord(f"invalid close status: {status!r}")
        if exit_code is not None and (
            not isinstance(exit_code, int) or isinstance(exit_code, bool)
        ):
            raise InvalidRecord("exit_code must be an integer or null")

        def finish(record: dict[str, Any]) -> None:
            if record["state"] == "closed":
                if (
                    record["close_status"] == status
                    and record["exit_code"] == exit_code
                ):
                    return
                raise ClosedLaunch(f"launch is already closed: {launch_id}")
            now = self._clock()
            record["state"] = "closed"
            record["state_changed_at"] = now
            record["updated_at"] = now
            record["closed_at"] = now
            record["close_status"] = status
            record["exit_code"] = exit_code

        return self._mutate(launch_id, finish, allow_closed=True)

    def record_failure(
        self,
        launch_id: str | None,
        *,
        stage: str,
        error: str,
        event_name: str | None = None,
    ) -> None:
        """Degrade a live record, or atomically preserve an orphan diagnostic."""

        failure = {
            "timestamp": self._clock(),
            "stage": str(stage),
            "event_name": event_name,
            "error": str(error)[:4096],
        }
        if launch_id is not None:
            try:

                def degrade(record: dict[str, Any]) -> None:
                    record["degraded"] = True
                    record["errors"] = (record["errors"] + [failure])[-50:]
                    record["updated_at"] = failure["timestamp"]

                self._mutate(launch_id, degrade)
                return
            except RegistryError:
                pass

        diagnostic = {
            "schema_version": SCHEMA_VERSION,
            "launch_id": launch_id,
            **failure,
        }
        try:
            with self._locked():
                self.failures_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                name = (
                    f"{failure['timestamp'].replace(':', '-')}-{uuid.uuid4().hex}.json"
                )
                self._write_unlocked(self.failures_dir / name, diagnostic)
        except OSError:
            # Hooks are observational and must never block their provider.
            return
