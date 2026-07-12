from __future__ import annotations

import json
import multiprocessing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from shuttlelib.sessions import (
    BindingConflict,
    ClosedLaunch,
    Registry,
    ResumeIdentityConflict,
)


class Clock:
    def __init__(self) -> None:
        self.tick = 0

    def __call__(self) -> str:
        self.tick += 1
        return f"2026-07-12T00:00:00.{self.tick:06d}Z"


def _record_process_failure(home: str, launch_id: str, index: int) -> None:
    Registry(home).record_failure(
        launch_id, stage="process-test", error=f"process failure {index}"
    )


def test_shuttle_home_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "override"
    monkeypatch.setenv("SHUTTLE_HOME", str(home))

    registry = Registry()
    record = registry.create_launch(provider="codex", mode="go", cwd=tmp_path)

    assert registry.home == home
    assert (home / "sessions" / f"{record['launch_id']}.json").is_file()


def test_schema_v1_launch_contains_launcher_and_lifecycle_fields(
    registry: Registry, launch: dict
) -> None:
    stored = registry.get(launch["launch_id"])

    assert stored == launch
    assert stored["schema_version"] == 1
    assert stored["provider_version"] == "0.144.1"
    assert stored["tmux_session"] == "shuttle-registry"
    assert stored["pane_id"] == "%7"
    assert stored["pid"] == 12345
    assert stored["state"] == "starting"
    assert stored["closed_at"] is None
    assert stored["close_status"] is None
    assert stored["exit_code"] is None


def test_native_binding_is_idempotent_and_immutable(
    registry: Registry, launch: dict
) -> None:
    registry.bind_native(launch["launch_id"], "native-1")
    registry.bind_native(launch["launch_id"], "native-1")

    with pytest.raises(BindingConflict):
        registry.bind_native(launch["launch_id"], "native-2")

    assert registry.get(launch["launch_id"])["native_session_id"] == "native-1"


def test_concurrent_binding_has_one_winner(registry: Registry, launch: dict) -> None:
    def bind(native_id: str) -> str:
        try:
            registry.bind_native(launch["launch_id"], native_id)
            return "bound"
        except BindingConflict:
            return "conflict"

    ids = [f"native-{index}" for index in range(16)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(bind, ids))

    assert outcomes.count("bound") == 1
    assert outcomes.count("conflict") == 15
    assert registry.get(launch["launch_id"])["native_session_id"] in ids


def test_concurrent_updates_never_lose_a_failure(
    registry: Registry, launch: dict
) -> None:
    def fail(index: int) -> None:
        registry.record_failure(
            launch["launch_id"], stage="test", error=f"failure {index}"
        )

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(fail, range(40)))

    record = registry.get(launch["launch_id"])
    assert record["degraded"] is True
    assert len(record["errors"]) == 40
    assert {item["error"] for item in record["errors"]} == {
        f"failure {index}" for index in range(40)
    }
    json.loads((registry.sessions_dir / f"{launch['launch_id']}.json").read_bytes())
    assert not list(registry.sessions_dir.glob("*.tmp"))


def test_directory_lock_serializes_separate_processes(
    registry: Registry, launch: dict
) -> None:
    context = multiprocessing.get_context("fork")
    processes = [
        context.Process(
            target=_record_process_failure,
            args=(str(registry.home), launch["launch_id"], index),
        )
        for index in range(12)
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)

    assert [process.exitcode for process in processes] == [0] * len(processes)
    record = registry.get(launch["launch_id"])
    assert len(record["errors"]) == 12
    assert {item["error"] for item in record["errors"]} == {
        f"process failure {index}" for index in range(12)
    }


def test_scan_skips_corrupt_and_partial_records(
    registry: Registry, launch: dict
) -> None:
    (registry.sessions_dir / "corrupt.json").write_text("{", encoding="utf-8")
    (registry.sessions_dir / "partial.json").write_text(
        '{"schema_version":1,"launch_id":"partial"}', encoding="utf-8"
    )
    (registry.sessions_dir / ".interrupted.json.tmp").write_text("{", encoding="utf-8")
    malformed = dict(launch)
    malformed["launch_id"] = "malformed"
    malformed["state"] = []
    (registry.sessions_dir / "malformed.json").write_text(
        json.dumps(malformed), encoding="utf-8"
    )
    conflicting_identity = dict(launch)
    conflicting_identity.update(
        launch_id="conflicting-identity",
        native_session_id="native-a",
        resume_of="native-b",
    )
    (registry.sessions_dir / "conflicting-identity.json").write_text(
        json.dumps(conflicting_identity), encoding="utf-8"
    )
    invalid_close = dict(launch)
    invalid_close.update(
        launch_id="invalid-close",
        state="closed",
        closed_at=launch["updated_at"],
        close_status="vanished",
    )
    (registry.sessions_dir / "invalid-close.json").write_text(
        json.dumps(invalid_close), encoding="utf-8"
    )

    result = registry.scan()

    assert [record["launch_id"] for record in result.records] == [launch["launch_id"]]
    assert {issue.path.name for issue in result.issues} == {
        "conflicting-identity.json",
        "corrupt.json",
        "invalid-close.json",
        "malformed.json",
        "partial.json",
    }


def test_closed_record_is_terminal(registry: Registry, launch: dict) -> None:
    closed = registry.close(launch["launch_id"], status="exited", exit_code=0)

    assert closed["state"] == "closed"
    assert closed["closed_at"] == closed["updated_at"]
    assert registry.close(launch["launch_id"], status="exited", exit_code=0) == closed
    with pytest.raises(ClosedLaunch):
        registry.transition(launch["launch_id"], "working")
    with pytest.raises(ClosedLaunch):
        registry.bind_native(launch["launch_id"], "native-after-close")
    with pytest.raises(ClosedLaunch):
        registry.close(launch["launch_id"], status="killed")


def test_resume_identity_and_newest_live_collapse(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "home", clock=Clock())
    first = registry.create_launch(
        launch_id="first", provider="codex", mode="go", cwd=tmp_path
    )
    registry.bind_native(first["launch_id"], "native-1")
    resumed = registry.create_launch(
        launch_id="resumed",
        provider="codex",
        mode="resume",
        cwd=tmp_path,
        resume_of="native-1",
    )
    registry.bind_native(resumed["launch_id"], "native-1", resumed=True)

    assert registry.latest_live_for_native("native-1")["launch_id"] == "resumed"
    collapsed = registry.list_launches(include_closed=False, collapse_native=True)
    assert [record["launch_id"] for record in collapsed] == ["resumed"]
    with pytest.raises(ResumeIdentityConflict):
        registry.bind_native(resumed["launch_id"], "native-other", resumed=True)


def test_resume_source_can_record_identity_when_launcher_only_knows_mode(
    registry: Registry,
) -> None:
    record = registry.create_launch(
        launch_id="resume-late",
        provider="codex",
        mode="resume",
        cwd="/tmp/project",
    )

    bound = registry.bind_native(record["launch_id"], "native-1", resumed=True)

    assert bound["native_session_id"] == "native-1"
    assert bound["resume_of"] == "native-1"
