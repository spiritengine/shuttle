from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

import pytest

from shuttlelib import codex_hook
from shuttlelib.sessions import Registry


def test_codex_01441_fixtures_drive_states_and_bind_native_id(
    registry: Registry, launch: dict, codex_payload
) -> None:
    environ = {"SHUTTLE_LAUNCH_ID": launch["launch_id"]}
    expected = [
        ("session_start", "starting"),
        ("user_prompt_submit", "working"),
        ("permission_request", "approval"),
        ("stop", "idle"),
    ]

    for fixture, state in expected:
        raw, _ = codex_payload(fixture)
        assert (
            codex_hook.handle(
                raw, environ=environ, registry=registry, which=lambda _: None
            )
            == 0
        )
        assert registry.get(launch["launch_id"])["state"] == state

    record = registry.get(launch["launch_id"])
    assert record["native_session_id"] == "019c1234-1111-7000-8000-000000000001"
    assert record["degraded"] is False


def test_permission_request_never_writes_a_decision(
    registry: Registry,
    launch: dict,
    codex_payload,
    capsys: pytest.CaptureFixture[str],
) -> None:
    raw, _ = codex_payload("permission_request")

    result = codex_hook.main(
        io.BytesIO(raw),
        environ={"SHUTTLE_LAUNCH_ID": launch["launch_id"]},
        registry=registry,
        which=lambda _: None,
    )

    assert result == 0
    assert capsys.readouterr().out == ""
    assert registry.get(launch["launch_id"])["state"] == "approval"


def test_forwarding_receives_exact_stdin_bytes(
    registry: Registry, launch: dict, codex_payload
) -> None:
    fixture, _ = codex_payload("user_prompt_submit")
    raw = fixture.rstrip(b"\n") + b"  \n"
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, b"", b"")

    environ = {
        "SHUTTLE_LAUNCH_ID": launch["launch_id"],
        "PATH": "/test/bin",
    }
    result = codex_hook.handle(
        raw,
        environ=environ,
        registry=registry,
        which=lambda name: "/test/bin/wt" if name == "wt" else None,
        runner=runner,
    )

    assert result == 0
    assert calls[0][0] == ["/test/bin/wt", "observe", "--hook", "codex"]
    assert calls[0][1]["input"] == raw
    assert calls[0][1]["env"] == environ
    assert calls[0][1]["stdout"] is subprocess.DEVNULL
    assert registry.get(launch["launch_id"])["degraded"] is False


def test_forwarding_failure_is_recorded_but_returns_zero(
    registry: Registry, launch: dict, codex_payload
) -> None:
    raw, _ = codex_payload("stop")

    def runner(command, **kwargs):
        return subprocess.CompletedProcess(command, 23, b"", b"observer broke")

    assert (
        codex_hook.handle(
            raw,
            environ={"SHUTTLE_LAUNCH_ID": launch["launch_id"]},
            registry=registry,
            which=lambda _: "/usr/bin/wt",
            runner=runner,
        )
        == 0
    )
    record = registry.get(launch["launch_id"])
    assert record["state"] == "idle"
    assert record["degraded"] is True
    assert record["errors"][-1]["stage"] == "wt_forward"
    assert "observer broke" in record["errors"][-1]["error"]


def test_absent_wt_is_an_optional_noop(
    registry: Registry, launch: dict, codex_payload
) -> None:
    raw, _ = codex_payload("stop")

    assert (
        codex_hook.handle(
            raw,
            environ={"SHUTTLE_LAUNCH_ID": launch["launch_id"]},
            registry=registry,
            which=lambda _: None,
            runner=lambda *args, **kwargs: pytest.fail("runner should not be called"),
        )
        == 0
    )
    assert registry.get(launch["launch_id"])["degraded"] is False


def test_binding_conflict_degrades_without_rebinding(
    registry: Registry, launch: dict, codex_payload
) -> None:
    registry.bind_native(launch["launch_id"], "native-original")
    raw, _ = codex_payload("session_start")

    assert (
        codex_hook.handle(
            raw,
            environ={"SHUTTLE_LAUNCH_ID": launch["launch_id"]},
            registry=registry,
            which=lambda _: None,
        )
        == 0
    )
    record = registry.get(launch["launch_id"])
    assert record["native_session_id"] == "native-original"
    assert record["degraded"] is True
    assert "BindingConflict" in record["errors"][-1]["error"]


@pytest.mark.parametrize(
    "fixture", ["user_prompt_submit", "permission_request", "stop"]
)
def test_non_start_event_cannot_drive_a_different_native_session(
    registry: Registry, launch: dict, codex_payload, fixture: str
) -> None:
    registry.bind_native(launch["launch_id"], "native-original")
    registry.transition(launch["launch_id"], "idle")
    raw, _ = codex_payload(fixture)
    payload = json.loads(raw)
    payload["session_id"] = "native-other"

    assert (
        codex_hook.handle(
            json.dumps(payload).encode(),
            environ={"SHUTTLE_LAUNCH_ID": launch["launch_id"]},
            registry=registry,
            which=lambda _: None,
        )
        == 0
    )
    record = registry.get(launch["launch_id"])
    assert record["native_session_id"] == "native-original"
    assert record["state"] == "idle"
    assert record["degraded"] is True
    assert "BindingConflict" in record["errors"][-1]["error"]


@pytest.mark.parametrize("raw", [b"", b"{", b"[]", b'{"no_event":true}'])
def test_bad_input_always_returns_zero_and_records_diagnostic(
    tmp_path: Path, raw: bytes
) -> None:
    registry = Registry(tmp_path / "home")

    assert (
        codex_hook.handle(raw, environ={}, registry=registry, which=lambda _: None) == 0
    )
    diagnostics = list(registry.failures_dir.glob("*.json"))
    assert len(diagnostics) == 1


def test_main_records_stdin_failure_and_returns_zero(tmp_path: Path) -> None:
    class BrokenInput:
        def read(self):
            raise OSError("stdin failed")

    registry = Registry(tmp_path / "home")

    assert codex_hook.main(BrokenInput(), registry=registry, environ={}) == 0
    diagnostics = list(registry.failures_dir.glob("*.json"))
    assert len(diagnostics) == 1
    assert '"stage":"codex_hook_stdin"' in diagnostics[0].read_text()


def test_main_returns_zero_when_registry_cannot_initialize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHUTTLE_HOME", "~shuttle-user-that-does-not-exist/home")

    assert codex_hook.main(io.BytesIO(b"{}")) == 0
