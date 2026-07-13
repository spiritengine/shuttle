"""Small command-line bridge between Shuttle's shell UI and its registry."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .sessions import Registry, RegistryError

HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "PermissionRequest", "Stop")


def hook_document() -> dict[str, Any]:
    handler = {
        "hooks": [
            {"type": "command", "command": "shuttle hook codex", "timeout": 12}
        ]
    }
    return {"hooks": {event: [handler] for event in HOOK_EVENTS}}


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
    actual = _load_json(hooks_path)
    expected = hook_document()
    messages: list[str] = []
    configured = actual == expected
    if not hooks_path.exists():
        messages.append(f"missing: {hooks_path}")
    elif not configured:
        messages.append(f"configuration differs: {hooks_path}")
    else:
        messages.append(f"configuration matches: {hooks_path}")

    try:
        config_text = config_path.read_text(encoding="utf-8")
    except OSError:
        config_text = ""
    trusted = configured
    for event in HOOK_EVENTS:
        trust_key = f'{hooks_path}:{event.lower()}:0:0'
        marker = f'[hooks.state."{trust_key}"]'
        if marker not in config_text or "trusted_hash" not in config_text.split(marker, 1)[1].split("\n[", 1)[0]:
            trusted = False
            messages.append(f"untrusted or not yet approved: {event}")
    if trusted:
        messages.append("all Shuttle Codex hooks are trusted")
    return configured and trusted, messages


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

    subs.add_parser("hooks-snippet")
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
        elif args.command == "hooks-snippet":
            print(json.dumps(hook_document(), indent=2))
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
