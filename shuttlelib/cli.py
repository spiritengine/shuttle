"""Small command-line bridge between Shuttle's shell UI and its registry."""

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from stat import S_IMODE
from typing import Any

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
class HookInstallResult:
    path: Path
    changed: bool
    backup_path: Path | None = None


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


def merge_hook_document(existing: dict[str, Any] | None) -> dict[str, Any]:
    """Return an additive hooks.json document with exactly one Shuttle hook per event."""

    document: dict[str, Any] = {} if existing is None else copy.deepcopy(existing)
    hooks = document.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise HookConfigError("top-level 'hooks' must be an object")

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


def _load_existing_hooks(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HookConfigError(f"malformed JSON in {path}: {exc}") from exc
    if not isinstance(existing, dict):
        raise HookConfigError(f"{path} must contain a JSON object")
    return existing


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_file(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _create_backup(path: Path, existing_stat: os.stat_result) -> Path:
    backup_path = path.with_name(f"{path.name}.shuttle.bak")
    if not backup_path.exists():
        shutil.copy2(path, backup_path)
        os.chown(backup_path, existing_stat.st_uid, existing_stat.st_gid)
        _fsync_file(backup_path)
        _fsync_directory(path.parent)
    return backup_path


def _write_json_atomic(
    path: Path, document: dict[str, Any], existing_stat: os.stat_result | None
) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temp_path = Path(temp_name)
    try:
        if existing_stat is not None:
            os.fchmod(fd, S_IMODE(existing_stat.st_mode))
            os.fchown(fd, existing_stat.st_uid, existing_stat.st_gid)
        else:
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(document, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        fd = -1
        os.replace(temp_path, path)
        _fsync_directory(path.parent)
    finally:
        if fd != -1:
            os.close(fd)
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def install_hooks(home: Path | None = None) -> HookInstallResult:
    home = Path.home() if home is None else home
    hooks_path = home / ".codex" / "hooks.json"
    existing = _load_existing_hooks(hooks_path)
    merged = merge_hook_document(existing)
    if existing == merged:
        return HookInstallResult(path=hooks_path, changed=False)

    existing_stat = hooks_path.stat() if hooks_path.exists() else None
    backup_path = _create_backup(hooks_path, existing_stat) if existing_stat else None
    _write_json_atomic(hooks_path, merged, existing_stat)
    return HookInstallResult(path=hooks_path, changed=True, backup_path=backup_path)


def find_shuttle_hook_locations(document: Any) -> tuple[dict[str, HookLocation], list[str]]:
    errors: list[str] = []
    locations: dict[str, HookLocation] = {}
    if not isinstance(document, dict):
        return locations, ["hooks.json must contain a JSON object"]
    hooks = document.get("hooks")
    if not isinstance(hooks, dict):
        return locations, ["top-level 'hooks' must be an object"]

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


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def hooks_diagnostics(home: Path | None = None) -> tuple[bool, list[str]]:
    home = Path.home() if home is None else home
    hooks_path = home / ".codex" / "hooks.json"
    config_path = home / ".codex" / "config.toml"
    messages: list[str] = []
    if not hooks_path.exists():
        messages.append(f"missing: {hooks_path}")
        return False, messages
    actual = _load_json(hooks_path)
    locations, errors = find_shuttle_hook_locations(actual)
    configured = not errors and len(locations) == len(HOOK_EVENTS)
    if configured:
        messages.append(f"configured: {hooks_path}")
    else:
        messages.append(f"configuration differs: {hooks_path}")
        messages.extend(errors)
        return False, messages

    try:
        config_text = config_path.read_text(encoding="utf-8")
    except OSError:
        config_text = ""
    untrusted: list[HookLocation] = []
    for event in HOOK_EVENTS:
        location = locations[event]
        trust_key = (
            f"{hooks_path}:{event.lower()}:{location.group_index}:{location.hook_index}"
        )
        marker = f'[hooks.state."{trust_key}"]'
        if marker not in config_text:
            untrusted.append(location)
            continue
        section = config_text.split(marker, 1)[1].split("\n[", 1)[0]
        if "trusted_hash" not in section:
            untrusted.append(location)
    if untrusted:
        for location in untrusted:
            messages.append(
                "configured but untrusted: "
                f"{location.event} at {location.group_index}:{location.hook_index}"
            )
    else:
        messages.append("all Shuttle Codex hooks are trusted")
    return configured and not untrusted, messages


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
