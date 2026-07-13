from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from shuttlelib.sessions import Registry

ROOT = Path(__file__).parents[1]
SHUTTLE = ROOT / "bin" / "shuttle"


def _executable(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def cli_env(tmp_path: Path) -> tuple[dict[str, str], Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    tmux_log = tmp_path / "tmux.log"
    _executable(
        bin_dir / "tmux",
        """import json, os, sys
args = sys.argv[1:]
with open(os.environ['TMUX_LOG'], 'a') as stream:
    stream.write(json.dumps(args) + '\\n')
sessions = [line.split('|') for line in os.environ.get('TMUX_SESSIONS', '').splitlines() if line]
cmd = args[0] if args else ''
if cmd in ('list-sessions', 'ls'):
    if not sessions:
        raise SystemExit(1)
    fmt = args[args.index('-F') + 1] if '-F' in args else ''
    for fields in sessions:
        name = fields[0]; attached = fields[1] if len(fields) > 1 else '0'; activity = fields[2] if len(fields) > 2 else '1'
        if 'session_attached' in fmt and 'session_activity' in fmt: print(f'{name}|{attached}|{activity}')
        elif 'session_activity' in fmt: print(f'{name}|{activity}')
        else: print(name)
elif cmd == 'has-session':
    target = args[args.index('-t') + 1].lstrip('=')
    raise SystemExit(0 if any(row[0] == target for row in sessions) else 1)
elif cmd == 'display-message':
    target = args[args.index('-t') + 1].lstrip('=')
    row = next((row for row in sessions if row[0] == target), None)
    print(row[1] if row and len(row) > 1 else '0')
elif cmd == 'capture-pane':
    print('> ')
""",
    )
    _executable(
        bin_dir / "skein",
        """import sys
if len(sys.argv) > 2 and sys.argv[1] == 'folio':
    print(f'folio {sys.argv[2]}')
    print('    # Provider Test')
""",
    )
    _executable(bin_dir / "codex", "import sys\nprint('codex-cli 0.144.1') if '--version' in sys.argv else None\n")
    _executable(bin_dir / "claude", "import sys\nprint('claude 1.0') if '--version' in sys.argv else None\n")
    _executable(bin_dir / "sleep", "pass\n")
    env = os.environ | {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path / "user"),
        "SHUTTLE_HOME": str(tmp_path / "shuttle-home"),
        "SHUTTLE_HEADLESS": "1",
        "TMUX_LOG": str(tmux_log),
        "TMUX_SESSIONS": "",
    }
    Path(env["HOME"]).mkdir()
    return env, tmux_log


def run_cli(env: dict[str, str], *args: str, input_bytes: bytes | None = None):
    return subprocess.run(
        [str(SHUTTLE), *args],
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        check=False,
    )


def tmux_calls(path: Path) -> list[list[str]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_go_provider_default_and_codex_initial_prompt(cli_env, tmp_path: Path) -> None:
    env, log = cli_env
    claude = run_cli(env, "go", "-d", str(tmp_path), "brief-1")
    assert claude.returncode == 0, claude.stderr.decode()
    first = Registry(env["SHUTTLE_HOME"]).list_launches()[-1]
    assert first["provider"] == "claude"
    assert first["tmux_session"] == "shuttle-provider-test"
    assert any(call[:2] == ["send-keys", "-t"] for call in tmux_calls(log))

    log.write_text("")
    codex = run_cli(env, "go", "--agent", "codex", "-d", str(tmp_path), "brief-1")
    assert codex.returncode == 0, codex.stderr.decode()
    second = Registry(env["SHUTTLE_HOME"]).list_launches()[-1]
    assert second["provider"] == "codex"
    assert second["tmux_session"] == "shuttle-codex-provider-test"
    new_session = next(call for call in tmux_calls(log) if call[0] == "new-session")
    assert "codex" in new_session[-1]
    assert "HANDOFF:\\ brief-1" in new_session[-1]
    assert not any(call[0] == "send-keys" for call in tmux_calls(log))
    assert b"degraded mode" in codex.stderr


def test_default_agent_environment_override(cli_env, tmp_path: Path) -> None:
    env, _ = cli_env
    env["SHUTTLE_DEFAULT_AGENT"] = "codex"
    result = run_cli(env, "go", "-d", str(tmp_path), "brief-2")
    assert result.returncode == 0
    assert Registry(env["SHUTTLE_HOME"]).list_launches()[-1]["provider"] == "codex"


def test_exact_target_wins_and_ambiguous_partial_errors(cli_env) -> None:
    env, log = cli_env
    env["TMUX_SESSIONS"] = "task|0|1\nshuttle-task-one|0|1\nshuttle-task-two|0|1"
    exact = run_cli(env, "peek", "task")
    assert exact.returncode == 0
    assert b"=== task" in exact.stdout
    assert "=task" in next(call for call in tmux_calls(log) if call[0] == "capture-pane")

    ambiguous = run_cli(env, "peek", "shuttle-task")
    assert ambiguous.returncode == 2
    assert b"Ambiguous session target" in ambiguous.stderr


@pytest.mark.parametrize(
    ("command", "tmux_command"),
    [("board", "attach"), ("peek", "capture-pane"), ("kill", "kill-session")],
)
def test_target_commands_use_resolved_exact_tmux_name(
    cli_env, command: str, tmux_command: str
) -> None:
    env, log = cli_env
    env["TMUX_SESSIONS"] = "shuttle-unique-name|0|1"
    result = run_cli(env, command, "unique")
    assert result.returncode == 0, result.stderr.decode()
    call = next(call for call in tmux_calls(log) if call[0] == tmux_command)
    assert call[call.index("-t") + 1] == "=shuttle-unique-name"


def test_status_prefers_codex_registry_state_and_separates_liveness(
    cli_env, tmp_path: Path
) -> None:
    env, _ = cli_env
    env["TMUX_SESSIONS"] = f"shuttle-codex-status|1|{int(os.path.getmtime(__file__))}"
    Registry(env["SHUTTLE_HOME"]).create_launch(
        provider="codex",
        mode="go",
        cwd=tmp_path,
        tmux_session="shuttle-codex-status",
        pane_id="%1",
        pid=os.getpid(),
    )

    result = run_cli(env, "status", "--agent", "codex")
    assert result.returncode == 0, result.stderr.decode()
    assert b"[codex]" in result.stdout
    assert b"attached; session=live process=live" in result.stdout
    assert b"starting" in result.stdout


def test_send_is_literal_and_codex_force_requires_idle(cli_env, tmp_path: Path) -> None:
    env, log = cli_env
    env["TMUX_SESSIONS"] = "shuttle-codex-safe|0|1"
    registry = Registry(env["SHUTTLE_HOME"])
    launch = registry.create_launch(
        provider="codex",
        mode="go",
        cwd=tmp_path,
        tmux_session="shuttle-codex-safe",
        pane_id="%3",
        pid=os.getpid(),
    )
    registry.transition(launch["launch_id"], "idle")
    marker = tmp_path / "interpreted"
    message = f"$(touch {marker}) `false` ; echo unsafe"
    sent = run_cli(env, "send", "shuttle-codex-safe", message)
    assert sent.returncode == 0, sent.stderr.decode()
    assert not marker.exists()
    literal = next(call for call in tmux_calls(log) if "-l" in call)
    assert literal[-2:] == ["--", message]

    registry.transition(launch["launch_id"], "working")
    refused = run_cli(env, "send", "--force", "shuttle-codex-safe", "try")
    assert refused.returncode == 1
    assert b"--force cannot override" in refused.stdout


def test_codex_send_refuses_idle_record_with_dead_process(cli_env, tmp_path: Path) -> None:
    env, _ = cli_env
    env["TMUX_SESSIONS"] = "shuttle-codex-dead|0|1"
    registry = Registry(env["SHUTTLE_HOME"])
    launch = registry.create_launch(
        provider="codex",
        mode="go",
        cwd=tmp_path,
        tmux_session="shuttle-codex-dead",
        pane_id="%4",
        pid=99999999,
    )
    registry.transition(launch["launch_id"], "idle")

    result = run_cli(env, "send", "--force", "shuttle-codex-dead", "no")
    assert result.returncode == 1
    assert b"registry state is 'dead'" in result.stdout


def test_hooks_snippet_and_doctor_never_write_dotfiles(cli_env) -> None:
    env, _ = cli_env
    hooks = Path(env["HOME"]) / ".codex" / "hooks.json"
    snippet = run_cli(env, "hooks")
    assert snippet.returncode == 0
    for event in (b"SessionStart", b"UserPromptSubmit", b"PermissionRequest", b"Stop"):
        assert event in snippet.stdout
    assert b"shuttle hook codex" in snippet.stdout
    assert not hooks.exists()

    doctor = run_cli(env, "hooks", "doctor")
    assert doctor.returncode == 1
    assert b"missing:" in doctor.stdout
    assert not hooks.exists()


def test_hook_entry_preserves_stdin_bytes_and_stdout(cli_env, tmp_path: Path) -> None:
    env, _ = cli_env
    wt_bytes = tmp_path / "wt-bytes"
    wt = Path(env["PATH"].split(":", 1)[0]) / "wt"
    _executable(
        wt,
        "import os,sys\nopen(os.environ['WT_BYTES'],'wb').write(sys.stdin.buffer.read())\n",
    )
    env["WT_BYTES"] = str(wt_bytes)
    raw = b'{"hook_event_name":"Stop","session_id":"native"}  \n'
    result = run_cli(env, "hook", "codex", input_bytes=raw)
    assert result.returncode == 0
    assert result.stdout == b""
    assert wt_bytes.read_bytes() == raw


def test_resume_infers_codex_identity_and_passes_explicit_prompt(
    cli_env, tmp_path: Path
) -> None:
    env, log = cli_env
    registry = Registry(env["SHUTTLE_HOME"])
    old = registry.create_launch(
        provider="codex",
        mode="go",
        cwd=tmp_path,
        tmux_session="old",
        brief="brief-9",
        title="Resume Me",
    )
    registry.bind_native(old["launch_id"], "019c-resume")
    registry.close(old["launch_id"], status="exited", exit_code=0)

    result = run_cli(env, "resume", "019c-resume", "continue carefully")
    assert result.returncode == 0, result.stderr.decode()
    current = registry.list_launches()[-1]
    assert current["provider"] == "codex"
    assert current["resume_of"] == "019c-resume"
    command = next(call for call in tmux_calls(log) if call[0] == "new-session")[-1]
    assert "codex resume 019c-resume" in command
    assert "continue\\ carefully" in command
    assert "--last" not in command


def test_raw_codex_uuid_requires_explicit_provider(cli_env, tmp_path: Path) -> None:
    env, _ = cli_env
    missing = run_cli(env, "resume", "unknown-uuid")
    assert missing.returncode == 1
    assert b"Raw Codex UUIDs require" in missing.stdout

    explicit = run_cli(
        env, "resume", "--agent", "codex", "--dir", str(tmp_path), "unknown-uuid"
    )
    assert explicit.returncode == 0
    assert Registry(env["SHUTTLE_HOME"]).list_launches()[-1]["resume_of"] == "unknown-uuid"


def test_claude_only_commands_refuse_codex(cli_env) -> None:
    env, _ = cli_env
    result = run_cli(env, "index", "--agent", "codex")
    assert result.returncode == 1
    assert b"Claude-only" in result.stdout
