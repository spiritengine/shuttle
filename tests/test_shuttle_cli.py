from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from stat import S_IMODE

import pytest

import shuttlelib.cli as shuttle_cli
from shuttlelib.cli import HOOK_COMMAND, HOOK_EVENTS
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
panes = [line.split('|') for line in os.environ.get('TMUX_PANES', '').splitlines() if line]
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
    fmt = args[-1] if args else ''
    if fmt == '#{session_name}':
        pane = next((row for row in panes if row[0] == target), None)
        if pane and len(pane) > 1:
            print(pane[1])
            raise SystemExit(0)
        row = next((row for row in sessions if row[0] == target), None)
        if row:
            print(row[0])
            raise SystemExit(0)
        raise SystemExit(1)
    row = next((row for row in sessions if row[0] == target), None)
    print(row[1] if row and len(row) > 1 else '0')
elif cmd == 'capture-pane':
    print('> ')
elif cmd == 'new-session':
    raise SystemExit(int(os.environ.get('TMUX_NEW_SESSION_RC', '0')))
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


def run_cli(
    env: dict[str, str],
    *args: str,
    input_bytes: bytes | None = None,
    timeout: float | None = None,
):
    return subprocess.run(
        [str(SHUTTLE), *args],
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        timeout=timeout,
        check=False,
    )


def tmux_calls(path: Path) -> list[list[str]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def install_gui_terminal(env: dict[str, str], tmp_path: Path) -> Path:
    terminal_log = tmp_path / "terminal.log"
    bin_dir = Path(env["PATH"].split(":", 1)[0])
    _executable(
        bin_dir / "gnome-terminal",
        "import json,os,sys\n"
        "with open(os.environ['TERMINAL_LOG'], 'a') as stream:\n"
        "    stream.write(json.dumps(sys.argv[1:]) + '\\n')\n",
    )
    env["TERMINAL_LOG"] = str(terminal_log)
    env["DISPLAY"] = ":99"
    env["SHUTTLE_SKIP_WINDOW_CHECK"] = "1"
    env.pop("SHUTTLE_HEADLESS", None)
    return terminal_log


def terminal_calls(path: Path) -> list[list[str]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def write_json(path: Path, document: dict) -> None:
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def install_fake_codex_app_server(env: dict[str, str]) -> None:
    bin_dir = Path(env["PATH"].split(":", 1)[0])
    _executable(
        bin_dir / "codex",
        r"""
import json
import os
import sys
import time
from pathlib import Path

if "--version" in sys.argv:
    print("codex-cli 0.144.1")
    raise SystemExit(0)
if sys.argv[1:] != ["app-server", "--stdio"]:
    raise SystemExit(0)

EVENTS = {
    "SessionStart": ("sessionStart", "session_start"),
    "UserPromptSubmit": ("userPromptSubmit", "user_prompt_submit"),
    "PermissionRequest": ("permissionRequest", "permission_request"),
    "Stop": ("stop", "stop"),
}


def emit(message):
    print(json.dumps(message), flush=True)


def hook_metadata(cwd):
    hooks_path = Path(os.environ["HOME"]) / ".codex" / "hooks.json"
    document = json.loads(hooks_path.read_text(encoding="utf-8"))
    status = os.environ.get("FAKE_CODEX_TRUST_STATUS", "trusted")
    enabled = os.environ.get("FAKE_CODEX_ENABLED", "1") != "0"
    hooks = []
    display_order = 0
    for event, groups in document.get("hooks", {}).items():
        event_name, event_key = EVENTS.get(event, (event[:1].lower() + event[1:], event.lower()))
        for group_index, group in enumerate(groups):
            for hook_index, hook in enumerate(group.get("hooks", [])):
                if hook.get("type") != "command":
                    continue
                hooks.append(
                    {
                        "key": f"{hooks_path}:{event_key}:{group_index}:{hook_index}",
                        "eventName": event_name,
                        "handlerType": "command",
                        "matcher": group.get("matcher"),
                        "command": hook.get("command"),
                        "timeoutSec": hook.get("timeout", 600),
                        "statusMessage": None,
                        "sourcePath": str(hooks_path),
                        "source": "user",
                        "pluginId": None,
                        "displayOrder": display_order,
                        "enabled": enabled,
                        "isManaged": False,
                        "currentHash": f"sha256:test-{display_order}",
                        "trustStatus": status,
                    }
                )
                display_order += 1
    return {"cwd": cwd, "hooks": hooks, "warnings": [], "errors": []}


for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if method == "initialize":
        emit(
            {
                "id": request["id"],
                "result": {
                    "userAgent": "fake",
                    "codexHome": os.environ.get("CODEX_HOME"),
                    "platformFamily": "unix",
                    "platformOs": "linux",
                },
            }
        )
    elif method == "initialized":
        continue
    elif method == "hooks/list":
        mode = os.environ.get("FAKE_CODEX_APP_SERVER_MODE", "ok")
        if mode == "timeout":
            time.sleep(30)
            continue
        if mode == "error":
            emit({"id": request["id"], "error": {"message": "boom"}})
            continue
        params = request.get("params") or {}
        cwd = (params.get("cwds") or [os.getcwd()])[0]
        if mode == "hook_errors":
            result = {
                "data": [
                    {
                        "cwd": cwd,
                        "hooks": [],
                        "warnings": [],
                        "errors": [{"path": "hooks.json", "message": "bad hook"}],
                    }
                ]
            }
        elif mode == "hook_warnings":
            result = {
                "data": [
                    {
                        "cwd": cwd,
                        "hooks": [],
                        "warnings": [
                            "/tmp/broken-hooks.json: missing field `command`"
                        ],
                        "errors": [],
                    }
                ]
            }
        else:
            result = {"data": [hook_metadata(cwd)]}
        emit({"id": request["id"], "result": result})
""",
    )


def hook_coords(document: dict, event: str) -> list[tuple[int, int]]:
    coords = []
    for group_index, group in enumerate(document["hooks"][event]):
        for hook_index, hook in enumerate(group["hooks"]):
            if hook == HOOK_COMMAND:
                coords.append((group_index, hook_index))
    return coords


def trust_section(hooks_path: Path, event: str, group_index: int, hook_index: int) -> str:
    key = f"{hooks_path}:{event.lower()}:{group_index}:{hook_index}"
    return f'[hooks.state."{key}"]\ntrusted_hash = "sha256-test"\n\n'


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


def test_go_closes_registry_record_when_tmux_new_session_fails(
    cli_env, tmp_path: Path
) -> None:
    env, _ = cli_env
    env["TMUX_NEW_SESSION_RC"] = "42"

    result = run_cli(env, "go", "-d", str(tmp_path), "brief-1")

    assert result.returncode == 42
    record = Registry(env["SHUTTLE_HOME"]).list_launches()[-1]
    assert record["state"] == "closed"
    assert record["close_status"] == "failed"
    assert record["exit_code"] == 42


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


@pytest.mark.parametrize("target", ["0", "-1"])
def test_numeric_targets_must_be_positive_before_indexing(cli_env, target: str) -> None:
    env, log = cli_env
    env["TMUX_SESSIONS"] = "shuttle-last|0|1"

    result = run_cli(env, "peek", target)

    assert result.returncode == 1
    assert b"Session number must be positive" in result.stderr
    assert not any(call[0] == "capture-pane" for call in tmux_calls(log))


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


def test_board_gui_uses_argv_terminal_and_exact_attach_target(
    cli_env, tmp_path: Path
) -> None:
    env, _ = cli_env
    env["TMUX_SESSIONS"] = "shuttle-valid name|0|1"
    terminal_log = install_gui_terminal(env, tmp_path)

    result = run_cli(env, "board", "shuttle-valid name")

    assert result.returncode == 0, result.stderr.decode()
    assert terminal_calls(terminal_log)[-1] == [
        "--title",
        "shuttle:shuttle-valid name",
        "--",
        "tmux",
        "attach",
        "-t",
        "=shuttle-valid name",
    ]


def test_go_reboard_gui_uses_exact_attach_target(cli_env, tmp_path: Path) -> None:
    env, _ = cli_env
    env["TMUX_SESSIONS"] = "shuttle-provider-test|0|1"
    terminal_log = install_gui_terminal(env, tmp_path)

    result = run_cli(env, "go", "-d", str(tmp_path), "brief-1")

    assert result.returncode == 0, result.stderr.decode()
    assert terminal_calls(terminal_log)[-1][-2:] == [
        "-t",
        "=shuttle-provider-test",
    ]


def test_go_gui_uses_exact_attach_target_after_launch(cli_env, tmp_path: Path) -> None:
    env, _ = cli_env
    terminal_log = install_gui_terminal(env, tmp_path)

    result = run_cli(env, "go", "-d", str(tmp_path), "brief-1")

    assert result.returncode == 0, result.stderr.decode()
    assert terminal_calls(terminal_log)[-1][-2:] == [
        "-t",
        "=shuttle-provider-test",
    ]


def test_resume_gui_uses_exact_attach_target_after_launch(
    cli_env, tmp_path: Path
) -> None:
    env, _ = cli_env
    terminal_log = install_gui_terminal(env, tmp_path)
    registry = Registry(env["SHUTTLE_HOME"])
    old = registry.create_launch(
        provider="codex",
        mode="go",
        cwd=tmp_path,
        tmux_session="old",
        title="Resume Me",
    )
    registry.bind_native(old["launch_id"], "019c-resume")
    registry.close(old["launch_id"], status="exited", exit_code=0)

    result = run_cli(env, "resume", "019c-resume")

    assert result.returncode == 0, result.stderr.decode()
    assert terminal_calls(terminal_log)[-1][-2:] == [
        "-t",
        "=shuttle-codex-resume-me",
    ]


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
    env["TMUX_PANES"] = "%3|shuttle-codex-safe"
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
    pane_check = next(
        call
        for call in tmux_calls(log)
        if call[:2] == ["display-message", "-t"] and call[2] == "%3"
    )
    assert pane_check[-1] == "#{session_name}"
    literal = next(call for call in tmux_calls(log) if "-l" in call)
    assert literal[literal.index("-t") + 1] == "%3"
    assert literal[-2:] == ["--", message]

    registry.transition(launch["launch_id"], "working")
    refused = run_cli(env, "send", "--force", "shuttle-codex-safe", "try")
    assert refused.returncode == 1
    assert b"--force cannot override" in refused.stdout


def test_codex_send_refuses_registry_pane_that_moved_sessions(
    cli_env, tmp_path: Path
) -> None:
    env, log = cli_env
    env["TMUX_SESSIONS"] = "shuttle-codex-safe|0|1\nother-session|0|1"
    env["TMUX_PANES"] = "%3|other-session"
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

    result = run_cli(env, "send", "shuttle-codex-safe", "nope")

    assert result.returncode == 1
    assert b"pane %3 belongs to 'other-session'" in result.stdout
    assert not any(call[0] == "send-keys" for call in tmux_calls(log))


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


def test_relay_is_claude_only_and_keeps_claude_send_path(
    cli_env, tmp_path: Path
) -> None:
    env, log = cli_env
    env["TMUX_SESSIONS"] = "shuttle-claude|0|1\nshuttle-codex-relay|0|1"
    registry = Registry(env["SHUTTLE_HOME"])
    registry.create_launch(
        provider="codex",
        mode="go",
        cwd=tmp_path,
        tmux_session="shuttle-codex-relay",
        pane_id="%9",
        pid=os.getpid(),
    )
    payload = tmp_path / "payload.txt"
    payload.write_text("hello from relay", encoding="utf-8")

    refused = run_cli(env, "relay", "shuttle-codex-relay", str(payload))
    assert refused.returncode == 1
    assert b"is not a claude session" in refused.stderr
    assert not any(call[0] == "send-keys" for call in tmux_calls(log))

    log.write_text("")
    sent = run_cli(env, "relay", "shuttle-claude", str(payload))
    assert sent.returncode == 0, sent.stderr.decode()
    literal = next(call for call in tmux_calls(log) if "-l" in call)
    assert literal[literal.index("-t") + 1] == "=shuttle-claude"
    assert literal[-2:] == ["--", "hello from relay"]


def test_hooks_snippet_and_doctor_never_write_dotfiles(cli_env) -> None:
    env, _ = cli_env
    hooks = Path(env["HOME"]) / ".codex" / "hooks.json"
    snippet = run_cli(env, "hooks")
    assert snippet.returncode == 0
    assert b"not a replacement" in snippet.stdout
    assert b"Exact ~/.codex/hooks.json" not in snippet.stdout
    for event in (b"SessionStart", b"UserPromptSubmit", b"PermissionRequest", b"Stop"):
        assert event in snippet.stdout
    assert b"shuttle hook codex" in snippet.stdout
    assert not hooks.exists()

    doctor = run_cli(env, "hooks", "doctor")
    assert doctor.returncode == 1
    assert b"missing:" in doctor.stdout
    assert not hooks.exists()


def test_hooks_install_creates_missing_hooks_file_without_config_writes(cli_env) -> None:
    env, _ = cli_env
    codex_dir = Path(env["HOME"]) / ".codex"
    hooks = codex_dir / "hooks.json"
    config = codex_dir / "config.toml"

    result = run_cli(env, "hooks", "install")

    assert result.returncode == 0, result.stderr.decode()
    assert b"updated:" in result.stdout
    assert hooks.exists()
    assert S_IMODE(hooks.stat().st_mode) == 0o600
    assert not config.exists()
    assert not hooks.with_name("hooks.json.shuttle.bak").exists()
    assert not list(codex_dir.glob("hooks.json.shuttle.*.bak"))
    installed = json.loads(hooks.read_text(encoding="utf-8"))
    for event in HOOK_EVENTS:
        assert hook_coords(installed, event) == [(0, 0)]


def test_hooks_install_additively_merges_and_preserves_existing_hooks(
    cli_env,
) -> None:
    env, _ = cli_env
    codex_dir = Path(env["HOME"]) / ".codex"
    codex_dir.mkdir()
    hooks = codex_dir / "hooks.json"
    config = codex_dir / "config.toml"
    config.write_text("sentinel = true\n", encoding="utf-8")
    sound_hook = {"type": "command", "command": "paplay /tmp/finished.wav"}
    unrelated_hook = {"type": "command", "command": "echo keep", "timeout": 3}
    stale_shuttle_hook = {
        "type": "command",
        "command": "shuttle hook codex",
        "timeout": 99,
        "extra": "stale",
    }
    existing = {
        "top_level": {"preserve": ["anything"]},
        "hooks": {
            "SessionStart": [
                {"matcher": "startup", "hooks": [unrelated_hook]},
            ],
            "PermissionRequest": [
                {"hooks": [HOOK_COMMAND, stale_shuttle_hook, unrelated_hook]},
            ],
            "Stop": [
                {"hooks": [sound_hook]},
            ],
            "OtherEvent": [
                {"matcher": "other", "hooks": [{"type": "command", "command": "true"}]},
            ],
        },
        "another_top_level_key": 42,
    }
    write_json(hooks, existing)
    hooks.chmod(0o640)
    original_bytes = hooks.read_bytes()
    before_stat = hooks.stat()

    result = run_cli(env, "hooks", "install")

    assert result.returncode == 0, result.stderr.decode()
    assert b"updated:" in result.stdout
    installed = json.loads(hooks.read_text(encoding="utf-8"))
    assert installed["top_level"] == existing["top_level"]
    assert installed["another_top_level_key"] == 42
    assert installed["hooks"]["OtherEvent"] == existing["hooks"]["OtherEvent"]
    assert installed["hooks"]["SessionStart"][0] == existing["hooks"]["SessionStart"][0]
    assert installed["hooks"]["SessionStart"][1] == {"hooks": [HOOK_COMMAND]}
    assert installed["hooks"]["PermissionRequest"][0]["hooks"] == [
        unrelated_hook,
        HOOK_COMMAND,
    ]
    assert installed["hooks"]["Stop"][0]["hooks"] == [sound_hook, HOOK_COMMAND]
    expected_coords = {
        "SessionStart": [(1, 0)],
        "UserPromptSubmit": [(0, 0)],
        "PermissionRequest": [(0, 1)],
        "Stop": [(0, 1)],
    }
    for event in HOOK_EVENTS:
        assert hook_coords(installed, event) == expected_coords[event]
        flattened = [
            hook
            for group in installed["hooks"][event]
            for hook in group["hooks"]
            if hook.get("command") == "shuttle hook codex"
        ]
        assert flattened == [HOOK_COMMAND]
    after_stat = hooks.stat()
    assert S_IMODE(after_stat.st_mode) == 0o640
    assert (after_stat.st_uid, after_stat.st_gid) == (
        before_stat.st_uid,
        before_stat.st_gid,
    )
    assert config.read_text(encoding="utf-8") == "sentinel = true\n"
    backups = list(codex_dir.glob("hooks.json.shuttle.*.bak"))
    assert len(backups) == 1
    backup = backups[0]
    assert backup.read_bytes() == original_bytes
    assert json.loads(backup.read_text(encoding="utf-8")) == existing
    assert not list(codex_dir.glob(".hooks.json.*.tmp"))

    first_bytes = hooks.read_bytes()
    second = run_cli(env, "hooks", "install")

    assert second.returncode == 0, second.stderr.decode()
    assert b"already installed:" in second.stdout
    assert hooks.read_bytes() == first_bytes
    assert list(codex_dir.glob("hooks.json.shuttle.*.bak")) == backups
    assert config.read_text(encoding="utf-8") == "sentinel = true\n"


def test_hooks_install_preserves_valid_existing_hook_shapes(cli_env) -> None:
    env, _ = cli_env
    codex_dir = Path(env["HOME"]) / ".codex"
    codex_dir.mkdir()
    hooks = codex_dir / "hooks.json"
    prompt_hook = {
        "type": "prompt",
        "prompt": "parsed by Codex, skipped by Shuttle",
        "futureField": {"preserve": True},
    }
    agent_hook = {
        "type": "agent",
        "agent": "worker",
        "futureField": ["preserve"],
    }
    command_hook = {
        "type": "command",
        "command": "echo keep",
        "timeout": 0,
        "statusMessage": "Keeping hook",
        "commandWindows": "cmd /c echo keep",
        "async": False,
        "futureField": 42,
    }
    existing = {
        "hooks": {
            "OtherEvent": [
                {
                    "matcher": "*",
                    "hooks": [prompt_hook, agent_hook, command_hook],
                    "groupFutureField": "preserve",
                }
            ]
        }
    }
    write_json(hooks, existing)

    result = run_cli(env, "hooks", "install")

    assert result.returncode == 0, result.stderr.decode()
    installed = json.loads(hooks.read_text(encoding="utf-8"))
    assert installed["hooks"]["OtherEvent"] == existing["hooks"]["OtherEvent"]
    for event in HOOK_EVENTS:
        assert hook_coords(installed, event) == [(0, 0)]


def test_hooks_doctor_uses_app_server_trust_not_config_toml(cli_env) -> None:
    env, _ = cli_env
    install_fake_codex_app_server(env)
    codex_dir = Path(env["HOME"]) / ".codex"
    codex_dir.mkdir()
    hooks = codex_dir / "hooks.json"
    config = codex_dir / "config.toml"
    sound_hook = {"type": "command", "command": "paplay /tmp/finished.wav"}
    document = {
        "hooks": {
            "SessionStart": [{"hooks": [HOOK_COMMAND]}],
            "UserPromptSubmit": [{"hooks": [HOOK_COMMAND]}],
            "PermissionRequest": [{"hooks": [HOOK_COMMAND]}],
            "Stop": [{"hooks": [sound_hook, HOOK_COMMAND]}],
        }
    }
    write_json(hooks, document)
    stale_toml = (
        "# stale comments and guessed trust records must not decide hook health\n"
        + trust_section(hooks, "SessionStart", 0, 0)
        + trust_section(hooks, "UserPromptSubmit", 0, 0)
        + trust_section(hooks, "PermissionRequest", 0, 0)
        + trust_section(hooks, "Stop", 0, 0)
    )
    config.write_text(stale_toml, encoding="utf-8")
    env["FAKE_CODEX_TRUST_STATUS"] = "untrusted"

    untrusted = run_cli(env, "hooks", "doctor")

    assert untrusted.returncode == 1
    assert b"configured:" in untrusted.stdout
    assert b"configured but untrusted: Stop at 0:1 (untrusted)" in untrusted.stdout
    assert b"all Shuttle Codex hooks are trusted" not in untrusted.stdout
    assert config.read_text(encoding="utf-8") == stale_toml

    config.write_text(
        "# no trusted_hash records here either; app-server is authoritative\n",
        encoding="utf-8",
    )
    env["FAKE_CODEX_TRUST_STATUS"] = "trusted"

    trusted = run_cli(env, "hooks", "doctor")

    assert trusted.returncode == 0, trusted.stderr.decode()
    assert b"all Shuttle Codex hooks are trusted and enabled" in trusted.stdout
    assert (
        config.read_text(encoding="utf-8")
        == "# no trusted_hash records here either; app-server is authoritative\n"
    )


def test_hooks_doctor_reports_unverified_without_app_server(cli_env) -> None:
    env, _ = cli_env
    codex_dir = Path(env["HOME"]) / ".codex"
    codex_dir.mkdir()
    hooks = codex_dir / "hooks.json"
    hook_document = {
        "hooks": {event: [{"hooks": [HOOK_COMMAND]}] for event in HOOK_EVENTS}
    }
    write_json(hooks, hook_document)

    result = run_cli(env, "hooks", "doctor")

    assert result.returncode == 1
    assert b"configured:" in result.stdout
    assert b"trust unverified:" in result.stdout
    assert json.loads(hooks.read_text(encoding="utf-8")) == hook_document


@pytest.mark.parametrize(
    ("mode", "needle"),
    [
        ("error", b"trust unverified: Codex app-server returned error: boom"),
        ("timeout", b"trust unverified: Codex app-server timed out"),
        ("hook_errors", b"trust unverified: Codex hooks/list returned hook errors"),
    ],
)
def test_hooks_doctor_reports_app_server_error_and_timeout(
    cli_env, mode: str, needle: bytes
) -> None:
    env, _ = cli_env
    install_fake_codex_app_server(env)
    env["FAKE_CODEX_APP_SERVER_MODE"] = mode
    env["SHUTTLE_HOOKS_APP_SERVER_TIMEOUT"] = "0.2"
    codex_dir = Path(env["HOME"]) / ".codex"
    codex_dir.mkdir()
    hooks = codex_dir / "hooks.json"
    write_json(
        hooks, {"hooks": {event: [{"hooks": [HOOK_COMMAND]}] for event in HOOK_EVENTS}}
    )

    result = run_cli(env, "hooks", "doctor", timeout=2)

    assert result.returncode == 1
    assert needle in result.stdout


def test_hooks_doctor_reports_app_server_hook_warnings(cli_env) -> None:
    env, _ = cli_env
    install_fake_codex_app_server(env)
    env["FAKE_CODEX_APP_SERVER_MODE"] = "hook_warnings"
    codex_dir = Path(env["HOME"]) / ".codex"
    codex_dir.mkdir()
    hooks = codex_dir / "hooks.json"
    write_json(
        hooks, {"hooks": {event: [{"hooks": [HOOK_COMMAND]}] for event in HOOK_EVENTS}}
    )

    result = run_cli(env, "hooks", "doctor")

    assert result.returncode == 1
    assert b"trust unverified: Codex hooks/list returned hook warnings" in result.stdout
    assert (
        b"hook warning: /tmp/broken-hooks.json: missing field `command`"
        in result.stdout
    )
    assert b"Codex did not report Shuttle hook" not in result.stdout


def test_hooks_doctor_requires_codex_enabled_status(cli_env) -> None:
    env, _ = cli_env
    install_fake_codex_app_server(env)
    env["FAKE_CODEX_ENABLED"] = "0"
    codex_dir = Path(env["HOME"]) / ".codex"
    codex_dir.mkdir()
    hooks = codex_dir / "hooks.json"
    write_json(
        hooks, {"hooks": {event: [{"hooks": [HOOK_COMMAND]}] for event in HOOK_EVENTS}}
    )

    result = run_cli(env, "hooks", "doctor")

    assert result.returncode == 1
    assert b"configured but disabled: SessionStart at 0:0" in result.stdout


def test_hooks_doctor_rejects_malformed_unknown_event(cli_env) -> None:
    env, _ = cli_env
    install_fake_codex_app_server(env)
    codex_dir = Path(env["HOME"]) / ".codex"
    codex_dir.mkdir()
    hooks = codex_dir / "hooks.json"
    body = b'{"hooks":{"UnknownEvent":[{"hooks":["not an object"]}]}}\n'
    hooks.write_bytes(body)

    result = run_cli(env, "hooks", "doctor")

    assert result.returncode == 1
    assert b"hooks.UnknownEvent[0].hooks[0] must be an object" in result.stdout


@pytest.mark.parametrize(
    ("name", "body"),
    [
        ("malformed", b'{"hooks": '),
        ("non_object", b'["not", "an", "object"]\n'),
    ],
)
def test_hooks_install_refuses_malformed_or_non_object_without_altering(
    cli_env, name: str, body: bytes
) -> None:
    env, _ = cli_env
    codex_dir = Path(env["HOME"]) / ".codex"
    codex_dir.mkdir()
    hooks = codex_dir / "hooks.json"
    config = codex_dir / "config.toml"
    hooks.write_bytes(body)
    hooks.chmod(0o600)
    config.write_text(f"name = {name!r}\n", encoding="utf-8")

    result = run_cli(env, "hooks", "install")

    assert result.returncode == 1
    assert hooks.read_bytes() == body
    assert S_IMODE(hooks.stat().st_mode) == 0o600
    assert config.read_text(encoding="utf-8") == f"name = {name!r}\n"
    assert not hooks.with_name("hooks.json.shuttle.bak").exists()
    assert not list(codex_dir.glob("hooks.json.shuttle.*.bak"))
    assert not list(codex_dir.glob(".hooks.json.*.tmp"))


def test_hooks_install_refuses_malformed_unknown_event_before_backup(cli_env) -> None:
    env, _ = cli_env
    codex_dir = Path(env["HOME"]) / ".codex"
    codex_dir.mkdir()
    hooks = codex_dir / "hooks.json"
    config = codex_dir / "config.toml"
    body = b'{"hooks":{"UnknownEvent":[{"hooks":["not an object"]}]}}\n'
    hooks.write_bytes(body)
    config.write_text("sentinel = true\n", encoding="utf-8")

    result = run_cli(env, "hooks", "install")

    assert result.returncode == 1
    assert b"hooks.UnknownEvent[0].hooks[0] must be an object" in result.stderr
    assert hooks.read_bytes() == body
    assert config.read_text(encoding="utf-8") == "sentinel = true\n"
    assert not list(codex_dir.glob("hooks.json.shuttle.*.bak"))
    assert not list(codex_dir.glob(".hooks.json.*.tmp"))


@pytest.mark.parametrize(
    ("document", "needle"),
    [
        (
            {"hooks": {"UnknownEvent": [{"hooks": [{"command": "echo no type"}]}]}},
            b"hooks.UnknownEvent[0].hooks[0].type is required",
        ),
        (
            {"hooks": {"SessionStart": [{"hooks": [{"type": "command"}]}]}},
            b"hooks.SessionStart[0].hooks[0].command is required for command hooks",
        ),
        (
            {
                "hooks": {
                    "SessionStart": [
                        {"hooks": [{"type": "command", "command": 5}]}
                    ]
                }
            },
            b"hooks.SessionStart[0].hooks[0].command must be a string",
        ),
        (
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "echo keep",
                                    "timeout": True,
                                }
                            ]
                        }
                    ]
                }
            },
            b"hooks.SessionStart[0].hooks[0].timeout must be a non-negative integer",
        ),
        (
            {"hooks": {"SessionStart": [{"matcher": 5, "hooks": []}]}},
            b"hooks.SessionStart[0].matcher must be a string",
        ),
    ],
)
def test_hooks_install_refuses_malformed_hook_objects_before_backup(
    cli_env, document: dict, needle: bytes
) -> None:
    env, _ = cli_env
    codex_dir = Path(env["HOME"]) / ".codex"
    codex_dir.mkdir()
    hooks = codex_dir / "hooks.json"
    config = codex_dir / "config.toml"
    body = (json.dumps(document, separators=(",", ":")) + "\n").encode()
    hooks.write_bytes(body)
    config.write_text("sentinel = true\n", encoding="utf-8")

    result = run_cli(env, "hooks", "install")

    assert result.returncode == 1
    assert needle in result.stderr
    assert hooks.read_bytes() == body
    assert config.read_text(encoding="utf-8") == "sentinel = true\n"
    assert not list(codex_dir.glob("hooks.json.shuttle.*.bak"))
    assert not list(codex_dir.glob(".hooks.json.*.tmp"))


@pytest.mark.parametrize("kind", ["symlink", "fifo", "directory"])
def test_hooks_install_refuses_nonregular_hooks_json_without_blocking(
    cli_env, tmp_path: Path, kind: str
) -> None:
    env, _ = cli_env
    codex_dir = Path(env["HOME"]) / ".codex"
    codex_dir.mkdir()
    hooks = codex_dir / "hooks.json"
    target = tmp_path / "target-hooks.json"
    target.write_text('{"hooks":{}}\n', encoding="utf-8")
    if kind == "symlink":
        os.symlink(target, hooks)
    elif kind == "fifo":
        os.mkfifo(hooks)
    else:
        hooks.mkdir()

    result = run_cli(env, "hooks", "install", timeout=2)

    assert result.returncode == 1
    assert result.stderr.startswith(b"shuttle hooks:")
    assert b"regular file" in result.stderr
    assert b"Traceback" not in result.stderr
    assert target.read_text(encoding="utf-8") == '{"hooks":{}}\n'
    assert not list(codex_dir.glob("hooks.json.shuttle.*.bak"))
    assert not list(codex_dir.glob(".hooks.json.*.tmp"))


def test_hooks_reader_refuses_device_file() -> None:
    device = Path("/dev/null")
    if not device.exists():
        pytest.skip("/dev/null is not available on this platform")
    with pytest.raises(shuttle_cli.HookConfigError, match="regular file"):
        shuttle_cli._read_existing_hooks_file(device)


def test_unique_backup_uses_o_excl_and_skips_directory_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hooks = tmp_path / "hooks.json"
    exact = b'{ "hooks": { "SessionStart": [] } }\n'
    hooks.write_bytes(exact)
    (tmp_path / "hooks.json.shuttle.collision.bak").mkdir()
    tokens = iter(["collision", "fresh"])
    monkeypatch.setattr(shuttle_cli.secrets, "token_hex", lambda _: next(tokens))
    existing = shuttle_cli.ExistingHooksFile({}, exact, hooks.stat())

    backup = shuttle_cli._create_backup(hooks, existing)

    assert backup == tmp_path / "hooks.json.shuttle.fresh.bak"
    assert backup.read_bytes() == exact
    assert (tmp_path / "hooks.json.shuttle.collision.bak").is_dir()


def test_concurrent_hooks_installs_lock_and_keep_exact_backup(cli_env) -> None:
    env, _ = cli_env
    codex_dir = Path(env["HOME"]) / ".codex"
    codex_dir.mkdir()
    hooks = codex_dir / "hooks.json"
    original = (
        b'{"hooks":{"SessionStart":[{"hooks":[{"type":"command",'
        b'"command":"echo keep"}]}]}}\n'
    )
    hooks.write_bytes(original)

    procs = [
        subprocess.Popen(
            [str(SHUTTLE), "hooks", "install"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        for _ in range(6)
    ]
    results = [proc.communicate(timeout=5) + (proc.returncode,) for proc in procs]

    assert all(returncode == 0 for _stdout, _stderr, returncode in results)
    assert any(b"updated:" in stdout for stdout, _stderr, _returncode in results)
    installed = json.loads(hooks.read_text(encoding="utf-8"))
    for event in HOOK_EVENTS:
        assert hook_coords(installed, event)
    backups = list(codex_dir.glob("hooks.json.shuttle.*.bak"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original
    assert not list(codex_dir.glob(".hooks.json.*.tmp"))


def test_hooks_install_reports_filesystem_errors_without_traceback(cli_env) -> None:
    env, _ = cli_env
    codex_path = Path(env["HOME"]) / ".codex"
    codex_path.write_text("not a directory\n", encoding="utf-8")

    result = run_cli(env, "hooks", "install")

    assert result.returncode == 1
    assert result.stderr.startswith(b"shuttle hooks:")
    assert b"Traceback" not in result.stderr


def test_hooks_install_reports_permission_errors_without_traceback(cli_env) -> None:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root can bypass the directory permission failure")
    env, _ = cli_env
    codex_dir = Path(env["HOME"]) / ".codex"
    codex_dir.mkdir()
    codex_dir.chmod(0o500)
    try:
        result = run_cli(env, "hooks", "install")
    finally:
        codex_dir.chmod(0o700)

    assert result.returncode == 1
    assert result.stderr.startswith(b"shuttle hooks:")
    assert b"Traceback" not in result.stderr


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


def test_resume_closes_registry_record_when_tmux_new_session_fails(
    cli_env, tmp_path: Path
) -> None:
    env, _ = cli_env
    registry = Registry(env["SHUTTLE_HOME"])
    old = registry.create_launch(
        provider="codex",
        mode="go",
        cwd=tmp_path,
        tmux_session="old",
        title="Resume Me",
    )
    registry.bind_native(old["launch_id"], "019c-resume")
    registry.close(old["launch_id"], status="exited", exit_code=0)
    env["TMUX_NEW_SESSION_RC"] = "43"

    result = run_cli(env, "resume", "019c-resume", "continue carefully")

    assert result.returncode == 43
    current = registry.list_launches()[-1]
    assert current["provider"] == "codex"
    assert current["resume_of"] == "019c-resume"
    assert current["state"] == "closed"
    assert current["close_status"] == "failed"
    assert current["exit_code"] == 43


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
