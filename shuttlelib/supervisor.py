"""Supervise one provider process and close its Shuttle launch record."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
from typing import Any

from .sessions import Registry


def _forward_closed(record: dict[str, Any], outcome: str) -> None:
    native_id = record.get("native_session_id")
    wt = shutil.which("wt")
    if not native_id or not wt:
        return
    try:
        subprocess.run(
            [
                wt,
                "observe",
                "--source",
                "codex",
                "--item",
                f"codex:{native_id}",
                "--kind",
                "closed",
                "--data",
                json.dumps({"status": outcome}, separators=(",", ":")),
                "--no-sound",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def supervise(
    launch_id: str,
    tmux_session: str,
    command: list[str],
    *,
    registry: Registry | None = None,
) -> int:
    registry = Registry() if registry is None else registry
    environ = os.environ.copy()
    environ["SHUTTLE_LAUNCH_ID"] = launch_id
    child: subprocess.Popen[Any] | None = None
    received_signal: int | None = None

    def relay(signum: int, _frame: Any) -> None:
        nonlocal received_signal
        received_signal = signum
        if child is not None and child.poll() is None:
            try:
                child.send_signal(signum)
            except ProcessLookupError:
                pass

    for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, relay)

    try:
        child = subprocess.Popen(command, env=environ)
        pane_id = environ.get("TMUX_PANE")
        if not pane_id:
            raise RuntimeError("TMUX_PANE is not set for supervised launch")
        registry.bind_location(
            launch_id,
            tmux_session=tmux_session,
            pane_id=pane_id,
            pid=child.pid,
        )
        returncode = child.wait()
        if received_signal is not None or returncode < 0:
            close_status, outcome = "killed", "killed"
        elif returncode == 0:
            close_status, outcome = "exited", "done"
        else:
            close_status, outcome = "failed", "failed"
        record = registry.close(
            launch_id, status=close_status, exit_code=returncode
        )
        _forward_closed(record, outcome)
        return returncode if returncode >= 0 else 128 + (-returncode)
    except BaseException as exc:
        try:
            registry.record_failure(
                launch_id, stage="supervisor", error=f"{type(exc).__name__}: {exc}"
            )
            record = registry.close(launch_id, status="failed", exit_code=127)
            _forward_closed(record, "failed")
        except BaseException:
            pass
        print(f"shuttle supervisor: {exc}", file=sys.stderr)
        return 127


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--launch-id", required=True)
    parser.add_argument("--tmux-session", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("provider command is required after --")
    return supervise(args.launch_id, args.tmux_session, command)


if __name__ == "__main__":
    raise SystemExit(main())
