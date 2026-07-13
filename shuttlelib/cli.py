"""Small command-line bridge between Shuttle's shell UI and its registry."""

from __future__ import annotations

import argparse
import copy
import errno
import fcntl
import json
import os
import secrets
import selectors
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from stat import (
    S_IMODE,
    S_ISBLK,
    S_ISCHR,
    S_ISDIR,
    S_ISFIFO,
    S_ISLNK,
    S_ISREG,
    S_ISSOCK,
)
from typing import Any, Callable

from .sessions import Registry, RegistryError

HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "PermissionRequest", "Stop")
HOOK_COMMAND = {
    "type": "command",
    "command": "shuttle hook codex",
    "timeout": 12,
}


class HookConfigError(ValueError):
    """Existing Codex hook config cannot be safely merged."""


@dataclass(frozen=True)
class HookLocation:
    event: str
    group_index: int
    hook_index: int


@dataclass(frozen=True)
class ExistingHooksFile:
    document: dict[str, Any]
    raw_bytes: bytes
    stat: os.stat_result


@dataclass(frozen=True)
class HookInstallResult:
    path: Path
    changed: bool
    backup_path: Path | None = None


@dataclass(frozen=True)
class HookTrustProbeResult:
    data: list[dict[str, Any]] | None = None
    error: str | None = None


TrustProbe = Callable[[Path, Path], HookTrustProbeResult]


def hook_document() -> dict[str, Any]:
    return {
        "hooks": {
            event: [{"hooks": [copy.deepcopy(HOOK_COMMAND)]}]
            for event in HOOK_EVENTS
        }
    }


def _is_shuttle_command_hook(hook: Any) -> bool:
    return (
        isinstance(hook, dict)
        and hook.get("type") == HOOK_COMMAND["type"]
        and hook.get("command") == HOOK_COMMAND["command"]
    )


def _is_exact_shuttle_hook(hook: Any) -> bool:
    return hook == HOOK_COMMAND


def _is_unconditional_group(group: dict[str, Any]) -> bool:
    return group.get("matcher") in (None, "")


def _hook_shape_errors(document: Any, *, require_hooks: bool) -> list[str]:
    if not isinstance(document, dict):
        return ["hooks.json must contain a JSON object"]
    hooks = document.get("hooks")
    if hooks is None:
        if require_hooks:
            return ["top-level 'hooks' must be an object"]
        return []
    if not isinstance(hooks, dict):
        return ["top-level 'hooks' must be an object"]

    errors: list[str] = []
    for event, groups in hooks.items():
        event_path = f"hooks.{event}"
        if not isinstance(groups, list):
            errors.append(f"{event_path} must be a list")
            continue
        for group_index, group in enumerate(groups):
            group_path = f"{event_path}[{group_index}]"
            if not isinstance(group, dict):
                errors.append(f"{group_path} must be an object")
                continue
            group_hooks = group.get("hooks")
            if not isinstance(group_hooks, list):
                errors.append(f"{group_path}.hooks must be a list")
                continue
            for hook_index, hook in enumerate(group_hooks):
                if not isinstance(hook, dict):
                    errors.append(
                        f"{group_path}.hooks[{hook_index}] must be an object"
                    )
    return errors


def _validate_hook_shapes(document: Any, *, require_hooks: bool = False) -> None:
    errors = _hook_shape_errors(document, require_hooks=require_hooks)
    if errors:
        raise HookConfigError(errors[0])


def merge_hook_document(existing: dict[str, Any] | None) -> dict[str, Any]:
    """Return an additive hooks.json document with exactly one Shuttle hook per event."""

    document: dict[str, Any] = {} if existing is None else copy.deepcopy(existing)
    _validate_hook_shapes(document)
    hooks = document.setdefault("hooks", {})

    for event in HOOK_EVENTS:
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            raise HookConfigError(f"hooks.{event} must be a list")

        target_group: dict[str, Any] | None = None
        for index, group in enumerate(groups):
            if not isinstance(group, dict):
                raise HookConfigError(f"hooks.{event}[{index}] must be an object")
            group_hooks = group.get("hooks")
            if not isinstance(group_hooks, list):
                raise HookConfigError(f"hooks.{event}[{index}].hooks must be a list")
            group["hooks"] = [
                hook for hook in group_hooks if not _is_shuttle_command_hook(hook)
            ]
            if target_group is None and _is_unconditional_group(group):
                target_group = group

        if target_group is None:
            target_group = {"hooks": []}
            groups.append(target_group)
        target_group["hooks"].append(copy.deepcopy(HOOK_COMMAND))

    return document


def _nonregular_kind(mode: int) -> str:
    if S_ISREG(mode):
        return "regular file"
    if S_ISDIR(mode):
        return "directory"
    if S_ISLNK(mode):
        return "symlink"
    if S_ISFIFO(mode):
        return "FIFO"
    if S_ISCHR(mode) or S_ISBLK(mode):
        return "device"
    if S_ISSOCK(mode):
        return "socket"
    return "non-regular file"


def _open_regular_no_follow(path: Path) -> tuple[int, os.stat_result] | None:
    try:
        path_stat = os.lstat(path)
    except FileNotFoundError:
        return None
    if not S_ISREG(path_stat.st_mode):
        raise HookConfigError(
            f"{path} must be a regular file, not {_nonregular_kind(path_stat.st_mode)}"
        )

    flags = os.O_RDONLY
    for flag_name in ("O_CLOEXEC", "O_NOFOLLOW", "O_NONBLOCK"):
        flags |= getattr(os, flag_name, 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EISDIR, errno.ENXIO):
            raise HookConfigError(f"{path} must be a regular file") from exc
        raise

    try:
        fd_stat = os.fstat(fd)
        if not S_ISREG(fd_stat.st_mode):
            raise HookConfigError(
                f"{path} must be a regular file, not {_nonregular_kind(fd_stat.st_mode)}"
            )
    except Exception:
        os.close(fd)
        raise
    return fd, fd_stat


def _read_all_fd(fd: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _read_existing_hooks_file(path: Path) -> ExistingHooksFile | None:
    opened = _open_regular_no_follow(path)
    if opened is None:
        return None
    fd, fd_stat = opened
    try:
        raw_bytes = _read_all_fd(fd)
    finally:
        os.close(fd)
    try:
        existing = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HookConfigError(f"malformed JSON in {path}: {exc}") from exc
    if not isinstance(existing, dict):
        raise HookConfigError(f"{path} must contain a JSON object")
    _validate_hook_shapes(existing)
    return ExistingHooksFile(existing, raw_bytes, fd_stat)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _revalidate_existing_hooks(path: Path, existing: ExistingHooksFile) -> None:
    current = _read_existing_hooks_file(path)
    if current is None:
        raise HookConfigError(f"{path} changed during install; retry")
    if not _same_file(current.stat, existing.stat) or current.raw_bytes != existing.raw_bytes:
        raise HookConfigError(f"{path} changed during install; retry")


def _ensure_missing_hooks_path(path: Path) -> None:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return
    raise HookConfigError(f"{path} appeared during install; retry")


def _write_all_fd(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written == 0:
            raise OSError("short write")
        view = view[written:]


def _create_backup(path: Path, existing: ExistingHooksFile) -> Path:
    mode = S_IMODE(existing.stat.st_mode)
    for _ in range(128):
        backup_path = path.with_name(
            f"{path.name}.shuttle.{secrets.token_hex(16)}.bak"
        )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(backup_path, flags, mode)
        except FileExistsError:
            continue
        try:
            os.fchmod(fd, mode)
            os.fchown(fd, existing.stat.st_uid, existing.stat.st_gid)
            _write_all_fd(fd, existing.raw_bytes)
            os.fsync(fd)
        except Exception:
            os.close(fd)
            try:
                backup_path.unlink()
            except FileNotFoundError:
                pass
            raise
        os.close(fd)
        _fsync_directory(path.parent)
        return backup_path
    raise HookConfigError(f"could not create a unique backup for {path}")


@contextmanager
def _install_lock(codex_dir: Path):
    codex_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = codex_dir / ".hooks.json.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(lock_path, flags, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _write_json_atomic(
    path: Path, document: dict[str, Any], existing: ExistingHooksFile | None
) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temp_path = Path(temp_name)
    try:
        if existing is not None:
            os.fchmod(fd, S_IMODE(existing.stat.st_mode))
            os.fchown(fd, existing.stat.st_uid, existing.stat.st_gid)
        else:
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(document, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        fd = -1
        if existing is not None:
            _revalidate_existing_hooks(path, existing)
            os.replace(temp_path, path)
        else:
            _ensure_missing_hooks_path(path)
            os.link(temp_path, path)
            temp_path.unlink()
        _fsync_directory(path.parent)
    finally:
        if fd != -1:
            os.close(fd)
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def _format_os_error(exc: OSError) -> str:
    path = exc.filename or exc.filename2
    detail = exc.strerror or str(exc)
    if path:
        return f"{path}: {detail}"
    return detail


def _install_hooks_locked(hooks_path: Path) -> HookInstallResult:
    existing = _read_existing_hooks_file(hooks_path)
    existing_document = None if existing is None else existing.document
    merged = merge_hook_document(existing_document)
    if existing_document == merged:
        return HookInstallResult(path=hooks_path, changed=False)

    backup_path = None
    if existing is not None:
        _revalidate_existing_hooks(hooks_path, existing)
        backup_path = _create_backup(hooks_path, existing)
    _write_json_atomic(hooks_path, merged, existing)
    return HookInstallResult(path=hooks_path, changed=True, backup_path=backup_path)


def install_hooks(home: Path | None = None) -> HookInstallResult:
    home = Path.home() if home is None else home
    hooks_path = home / ".codex" / "hooks.json"
    try:
        with _install_lock(hooks_path.parent):
            return _install_hooks_locked(hooks_path)
    except HookConfigError:
        raise
    except OSError as exc:
        raise HookConfigError(_format_os_error(exc)) from exc


def find_shuttle_hook_locations(document: Any) -> tuple[dict[str, HookLocation], list[str]]:
    errors: list[str] = []
    locations: dict[str, HookLocation] = {}
    shape_errors = _hook_shape_errors(document, require_hooks=True)
    if shape_errors:
        return locations, shape_errors
    hooks = document.get("hooks")

    for event in HOOK_EVENTS:
        event_locations: list[HookLocation] = []
        groups = hooks.get(event)
        if not isinstance(groups, list):
            errors.append(f"missing or malformed event: {event}")
            continue
        for group_index, group in enumerate(groups):
            if not isinstance(group, dict):
                errors.append(f"malformed matcher group: {event}[{group_index}]")
                continue
            group_hooks = group.get("hooks")
            if not isinstance(group_hooks, list):
                errors.append(f"malformed hooks list: {event}[{group_index}]")
                continue
            for hook_index, hook in enumerate(group_hooks):
                if _is_exact_shuttle_hook(hook):
                    event_locations.append(HookLocation(event, group_index, hook_index))
                elif _is_shuttle_command_hook(hook):
                    errors.append(
                        "different Shuttle hook command: "
                        f"{event} at {group_index}:{hook_index}"
                    )
        if len(event_locations) == 1:
            locations[event] = event_locations[0]
        elif not event_locations:
            errors.append(f"missing Shuttle hook: {event}")
        else:
            coords = ", ".join(
                f"{item.group_index}:{item.hook_index}" for item in event_locations
            )
            errors.append(f"duplicate Shuttle hooks: {event} at {coords}")
    return locations, errors


def _record_tsv(record: dict[str, Any]) -> str:
    fields = (
        "launch_id",
        "provider",
        "state",
        "tmux_session",
        "pane_id",
        "pid",
        "cwd",
        "brief",
        "title",
        "native_session_id",
        "resume_of",
        "degraded",
    )
    return "\x1f".join("" if record.get(key) is None else str(record[key]) for key in fields)


def _newest(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not records:
        return None
    return max(records, key=lambda item: (item["created_at"], item["launch_id"]))


def _codex_event_key(event: str) -> str:
    return {
        "SessionStart": "session_start",
        "UserPromptSubmit": "user_prompt_submit",
        "PermissionRequest": "permission_request",
        "Stop": "stop",
    }[event]


def _codex_event_name(event: str) -> str:
    return event[0].lower() + event[1:]


class AppServerProbeError(RuntimeError):
    pass


def _send_json_line(process: subprocess.Popen[str], message: dict[str, Any]) -> None:
    if process.stdin is None:
        raise AppServerProbeError("Codex app-server stdin is unavailable")
    process.stdin.write(json.dumps(message) + "\n")
    process.stdin.flush()


def _read_app_server_response(
    process: subprocess.Popen[str], request_id: int, deadline: float
) -> dict[str, Any]:
    if process.stdout is None:
        raise AppServerProbeError("Codex app-server stdout is unavailable")
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Codex app-server timed out")
            events = selector.select(remaining)
            if not events:
                raise TimeoutError("Codex app-server timed out")
            line = process.stdout.readline()
            if line == "":
                raise AppServerProbeError("Codex app-server exited before responding")
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AppServerProbeError("Codex app-server returned invalid JSON") from exc
            if message.get("id") != request_id:
                continue
            if "error" in message:
                error = message["error"]
                if isinstance(error, dict):
                    detail = error.get("message") or json.dumps(error, sort_keys=True)
                else:
                    detail = str(error)
                raise AppServerProbeError(f"Codex app-server returned error: {detail}")
            result = message.get("result")
            if not isinstance(result, dict):
                raise AppServerProbeError("Codex app-server returned a malformed response")
            return result
    finally:
        selector.close()


def codex_app_server_trust_probe(
    home: Path, cwd: Path, *, timeout_sec: float | None = None
) -> HookTrustProbeResult:
    if timeout_sec is None:
        try:
            timeout_sec = float(
                os.environ.get("SHUTTLE_HOOKS_APP_SERVER_TIMEOUT", "3.0")
            )
        except ValueError:
            timeout_sec = 3.0
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["CODEX_HOME"] = str(home / ".codex")
    deadline = time.monotonic() + timeout_sec
    process: subprocess.Popen[str] | None = None
    try:
        process = subprocess.Popen(
            ["codex", "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env=env,
        )
        _send_json_line(
            process,
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {
                        "name": "shuttle",
                        "title": "Shuttle",
                        "version": "0.1.0",
                    },
                    "capabilities": None,
                },
            },
        )
        _read_app_server_response(process, 1, deadline)
        _send_json_line(process, {"method": "initialized"})
        _send_json_line(
            process,
            {
                "id": 2,
                "method": "hooks/list",
                "params": {"cwds": [str(cwd)]},
            },
        )
        result = _read_app_server_response(process, 2, deadline)
        data = result.get("data")
        if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
            return HookTrustProbeResult(error="Codex hooks/list returned malformed data")
        return HookTrustProbeResult(data=data)
    except FileNotFoundError:
        return HookTrustProbeResult(error="Codex app-server is unavailable")
    except TimeoutError:
        return HookTrustProbeResult(error="Codex app-server timed out")
    except (OSError, AppServerProbeError) as exc:
        return HookTrustProbeResult(error=str(exc))
    finally:
        if process is not None:
            try:
                if process.stdin is not None:
                    process.stdin.close()
            except OSError:
                pass
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)


def _hooks_list_entry(
    probe_data: list[dict[str, Any]], cwd: Path
) -> tuple[dict[str, Any] | None, str | None]:
    cwd_text = str(cwd)
    for entry in probe_data:
        if entry.get("cwd") == cwd_text:
            return entry, None
    if len(probe_data) == 1:
        return probe_data[0], None
    return None, f"Codex hooks/list did not return cwd {cwd_text}"


def _find_codex_hook_metadata(
    hooks: list[Any], hooks_path: Path, location: HookLocation
) -> dict[str, Any] | None:
    expected_key = (
        f"{hooks_path}:{_codex_event_key(location.event)}:"
        f"{location.group_index}:{location.hook_index}"
    )
    for hook in hooks:
        if not isinstance(hook, dict):
            continue
        if (
            hook.get("key") == expected_key
            and hook.get("source") == "user"
            and hook.get("sourcePath") == str(hooks_path)
            and hook.get("eventName") == _codex_event_name(location.event)
            and hook.get("handlerType") == "command"
            and hook.get("command") == HOOK_COMMAND["command"]
        ):
            return hook
    return None


def hooks_diagnostics(
    home: Path | None = None,
    *,
    trust_probe: TrustProbe = codex_app_server_trust_probe,
    cwd: Path | None = None,
) -> tuple[bool, list[str]]:
    home = Path.home() if home is None else home
    hooks_path = home / ".codex" / "hooks.json"
    cwd = Path.cwd() if cwd is None else cwd
    messages: list[str] = []
    try:
        existing = _read_existing_hooks_file(hooks_path)
    except HookConfigError as exc:
        messages.append(f"configuration differs: {hooks_path}")
        messages.append(str(exc))
        return False, messages
    except OSError as exc:
        messages.append(f"configuration differs: {hooks_path}")
        messages.append(_format_os_error(exc))
        return False, messages
    if existing is None:
        messages.append(f"missing: {hooks_path}")
        return False, messages
    actual = existing.document
    locations, errors = find_shuttle_hook_locations(actual)
    configured = not errors and len(locations) == len(HOOK_EVENTS)
    if configured:
        messages.append(f"configured: {hooks_path}")
    else:
        messages.append(f"configuration differs: {hooks_path}")
        messages.extend(errors)
        return False, messages

    probe = trust_probe(home, cwd)
    if probe.error is not None or probe.data is None:
        detail = probe.error or "Codex hooks/list returned no data"
        messages.append(f"trust unverified: {detail}")
        return False, messages

    entry, entry_error = _hooks_list_entry(probe.data, cwd)
    if entry_error is not None or entry is None:
        messages.append(f"trust unverified: {entry_error}")
        return False, messages
    entry_errors = entry.get("errors")
    if entry_errors:
        messages.append("trust unverified: Codex hooks/list returned hook errors")
        for item in entry_errors:
            if isinstance(item, dict):
                message = item.get("message")
                path = item.get("path")
                if message and path:
                    messages.append(f"hook error: {path}: {message}")
                elif message:
                    messages.append(f"hook error: {message}")
        return False, messages
    codex_hooks = entry.get("hooks")
    if not isinstance(codex_hooks, list):
        messages.append("trust unverified: Codex hooks/list returned malformed hooks")
        return False, messages

    unhealthy: list[str] = []
    for event in HOOK_EVENTS:
        location = locations[event]
        metadata = _find_codex_hook_metadata(codex_hooks, hooks_path, location)
        if metadata is None:
            unhealthy.append(
                "trust unverified: Codex did not report Shuttle hook: "
                f"{location.event} at {location.group_index}:{location.hook_index}"
            )
            continue
        if not isinstance(metadata.get("currentHash"), str):
            unhealthy.append(
                "trust unverified: Codex did not report a hash for "
                f"{location.event} at {location.group_index}:{location.hook_index}"
            )
        if metadata.get("enabled") is not True:
            unhealthy.append(
                "configured but disabled: "
                f"{location.event} at {location.group_index}:{location.hook_index}"
            )
        trust_status = metadata.get("trustStatus")
        if trust_status != "trusted":
            unhealthy.append(
                "configured but untrusted: "
                f"{location.event} at {location.group_index}:{location.hook_index}"
                f" ({trust_status})"
            )
    if unhealthy:
        messages.extend(unhealthy)
    else:
        messages.append("all Shuttle Codex hooks are trusted and enabled")
    return configured and not unhealthy, messages


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m shuttlelib.cli")
    subs = parser.add_subparsers(dest="command", required=True)

    create = subs.add_parser("create")
    create.add_argument("--provider", required=True)
    create.add_argument("--mode", required=True)
    create.add_argument("--cwd", required=True)
    create.add_argument("--tmux-session", required=True)
    create.add_argument("--brief")
    create.add_argument("--title")
    create.add_argument("--resume-of")
    create.add_argument("--provider-version")

    bind = subs.add_parser("bind-location")
    bind.add_argument("launch_id")
    bind.add_argument("--tmux-session", required=True)
    bind.add_argument("--pane-id", required=True)
    bind.add_argument("--pid", required=True, type=int)

    get = subs.add_parser("get")
    get.add_argument("launch_id")
    get.add_argument("--tsv", action="store_true")

    lookup = subs.add_parser("lookup-tmux")
    lookup.add_argument("tmux_session")
    lookup.add_argument("--live", action="store_true")

    native = subs.add_parser("lookup-native")
    native.add_argument("native_session_id")

    records = subs.add_parser("records")
    records.add_argument("--live", action="store_true")

    failure = subs.add_parser("degrade")
    failure.add_argument("launch_id")
    failure.add_argument("message")

    close = subs.add_parser("close")
    close.add_argument("launch_id")
    close.add_argument("--status", required=True)
    close.add_argument("--exit-code", type=int)

    subs.add_parser("hooks-snippet")
    subs.add_parser("hooks-install")
    doctor = subs.add_parser("hooks-doctor")
    doctor.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    registry = Registry()
    try:
        if args.command == "create":
            record = registry.create_launch(
                provider=args.provider,
                provider_version=args.provider_version,
                mode=args.mode,
                cwd=args.cwd,
                tmux_session=args.tmux_session,
                brief=args.brief,
                title=args.title,
                resume_of=args.resume_of,
            )
            print(record["launch_id"])
        elif args.command == "bind-location":
            registry.bind_location(
                args.launch_id,
                tmux_session=args.tmux_session,
                pane_id=args.pane_id,
                pid=args.pid,
            )
        elif args.command == "get":
            record = registry.get(args.launch_id)
            print(_record_tsv(record) if args.tsv else json.dumps(record))
        elif args.command == "lookup-tmux":
            matches = [
                record
                for record in registry.list_launches(include_closed=not args.live)
                if record["tmux_session"] == args.tmux_session
            ]
            record = _newest(matches)
            if record is None:
                return 1
            print(_record_tsv(record))
        elif args.command == "lookup-native":
            matches = [
                record
                for record in registry.list_launches()
                if record["native_session_id"] == args.native_session_id
                or record["resume_of"] == args.native_session_id
            ]
            record = _newest(matches)
            if record is None:
                return 1
            print(_record_tsv(record))
        elif args.command == "records":
            for record in registry.list_launches(
                include_closed=not args.live, collapse_native=True
            ):
                print(_record_tsv(record))
        elif args.command == "degrade":
            registry.record_failure(
                args.launch_id, stage="launcher", error=args.message
            )
        elif args.command == "close":
            registry.close(
                args.launch_id, status=args.status, exit_code=args.exit_code
            )
        elif args.command == "hooks-snippet":
            print(json.dumps(hook_document(), indent=2))
        elif args.command == "hooks-install":
            result = install_hooks()
            if result.changed:
                print(f"updated: {result.path}")
                if result.backup_path is not None:
                    print(f"backup: {result.backup_path}")
            else:
                print(f"already installed: {result.path}")
        elif args.command == "hooks-doctor":
            healthy, messages = hooks_diagnostics()
            if not args.quiet:
                print("Codex hook configuration (read-only):")
                for message in messages:
                    print(f"  {message}")
            return 0 if healthy else 1
    except RegistryError as exc:
        print(f"shuttle registry: {exc}", file=sys.stderr)
        return 1
    except HookConfigError as exc:
        print(f"shuttle hooks: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
