from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from shuttlelib import supervisor
from shuttlelib.sessions import Registry


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@pytest.mark.parametrize(
    ("child", "status", "exit_code", "supervisor_code"),
    [
        ("raise SystemExit(0)", "exited", 0, 0),
        ("raise SystemExit(7)", "failed", 7, 7),
        ("import os,signal; os.kill(os.getpid(), signal.SIGTERM)", "killed", -15, 143),
    ],
)
def test_supervisor_binds_location_and_maps_process_outcome(
    tmp_path: Path,
    child: str,
    status: str,
    exit_code: int,
    supervisor_code: int,
) -> None:
    home = tmp_path / "home"
    registry = Registry(home)
    launch = registry.create_launch(
        provider="codex",
        mode="go",
        cwd=tmp_path,
        tmux_session="shuttle-codex-test",
    )
    env = os.environ | {
        "SHUTTLE_HOME": str(home),
        "TMUX_PANE": "%42",
    }

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "shuttlelib.supervisor",
            "--launch-id",
            launch["launch_id"],
            "--tmux-session",
            "shuttle-codex-test",
            "--",
            sys.executable,
            "-c",
            child,
        ],
        env=env,
        check=False,
    )

    record = registry.get(launch["launch_id"])
    assert result.returncode == supervisor_code
    assert record["pane_id"] == "%42"
    assert isinstance(record["pid"], int)
    assert record["state"] == "closed"
    assert record["close_status"] == status
    assert record["exit_code"] == exit_code


def test_supervisor_forwards_closed_only_after_native_identity_is_bound(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    wt_log = tmp_path / "wt.json"
    wt = bin_dir / "wt"
    wt.write_text(
        "#!/usr/bin/env python3\n"
        "import json,os,sys\n"
        "open(os.environ['WT_LOG'],'w').write(json.dumps(sys.argv[1:]))\n"
    )
    wt.chmod(0o755)
    home = tmp_path / "home"
    registry = Registry(home)
    launch = registry.create_launch(
        provider="codex", mode="go", cwd=tmp_path, tmux_session="shuttle-codex-test"
    )
    registry.bind_native(launch["launch_id"], "native-123")
    env = os.environ | {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "SHUTTLE_HOME": str(home),
        "TMUX_PANE": "%8",
        "WT_LOG": str(wt_log),
    }

    subprocess.run(
        [
            sys.executable,
            "-m",
            "shuttlelib.supervisor",
            "--launch-id",
            launch["launch_id"],
            "--tmux-session",
            "shuttle-codex-test",
            "--",
            sys.executable,
            "-c",
            "pass",
        ],
        env=env,
        check=True,
    )

    args = json.loads(wt_log.read_text())
    assert args[:6] == [
        "observe",
        "--source",
        "codex",
        "--item",
        "codex:native-123",
        "--kind",
    ]
    assert "closed" in args
    assert '{"status":"done"}' in args


def test_supervisor_kills_child_before_closing_after_post_spawn_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingRegistry:
        pid: int | None = None
        failure_recorded = False
        closed = False

        def bind_location(
            self, launch_id: str, *, tmux_session: str, pane_id: str, pid: int
        ) -> None:
            self.pid = pid
            raise RuntimeError("bind failed")

        def record_failure(
            self,
            launch_id: str,
            *,
            stage: str,
            error: str,
            event_name: str | None = None,
        ) -> None:
            self.failure_recorded = True

        def close(
            self, launch_id: str, *, status: str, exit_code: int | None = None
        ) -> dict[str, object]:
            self.closed = True
            assert status == "failed"
            assert exit_code == 127
            return {}

    registry = FailingRegistry()
    monkeypatch.setenv("TMUX_PANE", "%42")
    monkeypatch.setattr(supervisor, "CHILD_STOP_TIMEOUT", 0.1)

    result = supervisor.supervise(
        "launch-id",
        "tmux-session",
        [
            sys.executable,
            "-c",
            "import signal,time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "time.sleep(60)",
        ],
        registry=registry,  # type: ignore[arg-type]
    )

    assert result == 127
    assert registry.failure_recorded
    assert registry.closed
    assert registry.pid is not None
    assert not _pid_exists(registry.pid)
