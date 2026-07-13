from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from shuttlelib.sessions import Registry


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
