"""Callable Codex hook observer for Shuttle launch records."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from typing import Any, BinaryIO, Callable, Mapping

from .sessions import Registry

_EVENT_STATES = {
    "SessionStart": "starting",
    "UserPromptSubmit": "working",
    "PermissionRequest": "approval",
    "Stop": "idle",
}


def _error_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _record_failure(
    registry: Registry,
    launch_id: str | None,
    *,
    stage: str,
    error: str,
    event_name: str | None,
) -> None:
    try:
        registry.record_failure(
            launch_id, stage=stage, error=error, event_name=event_name
        )
    except BaseException:
        # This process is in Codex's control path. Even a broken disk or a
        # programming error in diagnostic handling cannot affect Codex.
        pass


def _observe_event(
    raw: bytes,
    *,
    environ: Mapping[str, str],
    registry: Registry,
) -> tuple[str | None, str | None]:
    launch_id = environ.get("SHUTTLE_LAUNCH_ID") or None
    event_name: str | None = None
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("hook payload must be a JSON object")
        event = payload.get("hook_event_name")
        if not isinstance(event, str):
            raise ValueError("hook_event_name must be a string")
        event_name = event
        if event not in _EVENT_STATES:
            raise ValueError(f"unsupported hook event: {event}")
        if launch_id is None:
            raise ValueError("SHUTTLE_LAUNCH_ID is not set")
        native_id = payload.get("session_id")
        if not isinstance(native_id, str) or not native_id:
            raise ValueError(f"{event} session_id must be a non-empty string")
        # Every supported Codex hook carries the native session id. Revalidate
        # the environment join on every event so a stale/misrouted hook cannot
        # drive another launch's state after SessionStart bound its identity.
        registry.bind_native(
            launch_id,
            native_id,
            resumed=event == "SessionStart" and payload.get("source") == "resume",
        )
        registry.transition(launch_id, _EVENT_STATES[event])
    except BaseException as exc:
        _record_failure(
            registry,
            launch_id,
            stage="codex_hook",
            error=_error_text(exc),
            event_name=event_name,
        )
    return launch_id, event_name


def _forward_to_wt(
    raw: bytes,
    *,
    environ: Mapping[str, str],
    registry: Registry,
    launch_id: str | None,
    event_name: str | None,
    which: Callable[[str], str | None],
    runner: Callable[..., Any],
) -> None:
    try:
        wt = which("wt")
    except BaseException as exc:
        _record_failure(
            registry,
            launch_id,
            stage="wt_lookup",
            error=_error_text(exc),
            event_name=event_name,
        )
        return
    if wt is None:
        return
    try:
        completed = runner(
            [wt, "observe", "--hook", "codex"],
            input=raw,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False,
            timeout=10,
            env=dict(environ),
        )
        returncode = getattr(completed, "returncode", 0)
        if returncode:
            stderr = getattr(completed, "stderr", b"") or b""
            if isinstance(stderr, bytes):
                detail = stderr.decode("utf-8", errors="replace")
            else:
                detail = str(stderr)
            raise RuntimeError(f"wt exited {returncode}: {detail[:1000]}")
    except BaseException as exc:
        _record_failure(
            registry,
            launch_id,
            stage="wt_forward",
            error=_error_text(exc),
            event_name=event_name,
        )


def handle(
    raw: bytes,
    *,
    environ: Mapping[str, str] | None = None,
    registry: Registry | None = None,
    which: Callable[[str], str | None] = shutil.which,
    runner: Callable[..., Any] = subprocess.run,
) -> int:
    """Observe and forward one exact Codex hook payload; always return zero."""

    try:
        actual_environ = os.environ if environ is None else environ
        actual_registry = Registry() if registry is None else registry
        launch_id: str | None = actual_environ.get("SHUTTLE_LAUNCH_ID") or None
        event_name: str | None = None
        try:
            launch_id, event_name = _observe_event(
                raw, environ=actual_environ, registry=actual_registry
            )
        except BaseException as exc:
            _record_failure(
                actual_registry,
                launch_id,
                stage="codex_hook_internal",
                error=_error_text(exc),
                event_name=event_name,
            )
        try:
            _forward_to_wt(
                raw,
                environ=actual_environ,
                registry=actual_registry,
                launch_id=launch_id,
                event_name=event_name,
                which=which,
                runner=runner,
            )
        except BaseException as exc:
            _record_failure(
                actual_registry,
                launch_id,
                stage="wt_forward_internal",
                error=_error_text(exc),
                event_name=event_name,
            )
    except BaseException:
        pass
    return 0


def main(
    stdin: BinaryIO | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    registry: Registry | None = None,
    which: Callable[[str], str | None] = shutil.which,
    runner: Callable[..., Any] = subprocess.run,
) -> int:
    """CLI entry point suitable for every configured Codex command hook."""

    actual_environ = environ
    actual_registry = registry
    try:
        actual_environ = os.environ if environ is None else environ
        actual_registry = Registry() if registry is None else registry
        stream = sys.stdin.buffer if stdin is None else stdin
        raw = stream.read()
    except BaseException as exc:
        if actual_registry is not None:
            launch_id = (
                actual_environ.get("SHUTTLE_LAUNCH_ID") or None
                if actual_environ is not None
                else None
            )
            _record_failure(
                actual_registry,
                launch_id,
                stage="codex_hook_stdin",
                error=_error_text(exc),
                event_name=None,
            )
        return 0
    try:
        return handle(
            raw,
            environ=actual_environ,
            registry=actual_registry,
            which=which,
            runner=runner,
        )
    except BaseException:
        # Last-resort guarantee: a Shuttle observer never blocks Codex.
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
