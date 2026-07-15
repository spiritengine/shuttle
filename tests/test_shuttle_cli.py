from __future__ import annotations

import json
import os
import subprocess
import time
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
        # Models tmux 3.0a target semantics, measured against the real binary:
        #   - session targets resolve exact name FIRST, then fall back to PREFIX match
        #   - has-session / kill-session HONOUR the '=' exact-match prefix
        #   - pane targets (send-keys, capture-pane, display-message) REJECT '=name'
        #   - list-panes IGNORES '=' and still prefix-matches (a real 3.0a quirk)
        # The prefix fallback is the point: a fake that merely refuses an inexact
        # target cannot catch code that lets tmux silently guess the wrong session.
        """import json, os, sys
args = sys.argv[1:]
with open(os.environ['TMUX_LOG'], 'a') as stream:
    stream.write(json.dumps(args) + '\\n')

state = os.environ['TMUX_LOG'] + '.sessions'
# TMUX_SESSIONS is a convenient '|'-delimited table, which by construction cannot
# express a session name containing '|', a newline, a backslash or the 0x1f
# separator — all of which tmux accepts. TMUX_SESSIONS_JSON exists so those names
# ARE expressible: without it, a parser that splits on '|' can never be tested
# against the name that breaks it, and the fake inherits the bug it should catch.
if os.environ.get('TMUX_SESSIONS_JSON'):
    sessions = [list(row) for row in json.loads(os.environ['TMUX_SESSIONS_JSON'])]
else:
    sessions = [line.split('|') for line in os.environ.get('TMUX_SESSIONS', '').splitlines() if line]
if os.path.exists(state):
    for line in open(state).read().splitlines():
        if line and not any(row[0] == line.split('|')[0] for row in sessions):
            sessions.append(line.split('|'))
panes = [line.split('|') for line in os.environ.get('TMUX_PANES', '').splitlines() if line]
pstate = os.environ['TMUX_LOG'] + '.panes'
if os.path.exists(pstate):
    for line in open(pstate).read().splitlines():
        if line:
            panes.append(line.split('|'))

def find_session(target, exact_only):
    # tmux resolution order for a session target: exact name, then start-of-name.
    for row in sessions:
        if row[0] == target:
            return row[0]
    if exact_only:
        return None
    for row in sessions:
        if row[0].startswith(target):
            return row[0]
    return None

def pane_of(name):
    for index, row in enumerate(sessions):
        if row[0] == name:
            return '%' + str(index + 1)
    return None

def session_of(pane):
    for row in panes:
        if row[0] == pane:
            return row[1] if len(row) > 1 else None
    for index, row in enumerate(sessions):
        if '%' + str(index + 1) == pane:
            return row[0]
    return None

def pane_target(target):
    if target.startswith('='):
        sys.stderr.write("can't find pane: " + target + '\\n')
        raise SystemExit(1)
    if target == '':
        # Real tmux NEVER fails on an empty -t: it binds the current session (or the
        # most recently used one). Modelling that forgiveness is the whole point — a
        # fake that merely refused could not catch code that leaks an empty target,
        # which is how a command ends up reading the CALLER's session instead.
        current = os.environ.get('TMUX_CURRENT_SESSION', '')
        resolved = pane_of(current) if current else None
    elif target.startswith('%'):
        resolved = target if session_of(target) is not None else None
    else:
        # A bare session name on a pane target resolves to that session's active
        # pane — and slides to a PREFIX match if the exact name is gone.
        name = find_session(target, exact_only=False)
        resolved = pane_of(name) if name else None
    if resolved is None:
        sys.stderr.write("can't find pane: " + target + '\\n')
        raise SystemExit(1)
    return resolved

cmd = args[0] if args else ''
if cmd in ('list-sessions', 'ls'):
    if not sessions:
        raise SystemExit(1)
    fmt = args[args.index('-F') + 1] if '-F' in args else ''
    for fields in sessions:
        name = fields[0]; attached = fields[1] if len(fields) > 1 else '0'; activity = fields[2] if len(fields) > 2 else '1'
        if not fmt:
            print(name)
            continue
        # Honour the caller's real format string, separator and all — otherwise a
        # lookup that splits on 0x1f can't be tested at all.
        print(fmt.replace('#{session_name}', name)
                 .replace('#{session_attached}', attached)
                 .replace('#{session_activity}', activity))
elif cmd == 'has-session':
    target = args[args.index('-t') + 1]
    # Honours '=' : exact only. Without it, a prefix match counts.
    found = find_session(target[1:], True) if target.startswith('=') else find_session(target, False)
    raise SystemExit(0 if found else 1)
elif cmd == 'kill-session':
    target = args[args.index('-t') + 1]
    found = find_session(target[1:], True) if target.startswith('=') else find_session(target, False)
    if not found:
        sys.stderr.write("can't find session: " + target + '\\n')
        raise SystemExit(1)
elif cmd == 'list-panes':
    target = args[args.index('-t') + 1]
    # tmux 3.0a IGNORES '=' here and still prefix-matches. Reproducing that is the
    # whole point: it is why session_pane() must verify #{session_name} itself
    # rather than trusting '=' to have enforced exactness.
    name = find_session(target.lstrip('='), exact_only=False)
    # TMUX_PANELESS models a session that vanished between resolve and use: it is
    # still listed, but has no pane to bind. This is the state every empty-'-t'
    # guard exists to handle.
    if name in os.environ.get('TMUX_PANELESS', '').split(','):
        raise SystemExit(0)
    pane = pane_of(name) if name else None
    if pane is None:
        sys.stderr.write("can't find session: " + target + '\\n')
        raise SystemExit(1)
    fmt = args[args.index('-F') + 1] if '-F' in args else ''
    fmt = fmt.replace('#{session_name}', name)
    def emit(window_active, pane_active, pane_id):
        print(fmt.replace('#{window_active}', window_active)
                 .replace('#{pane_active}', pane_active)
                 .replace('#{pane_id}', pane_id))
    # A multi-window, multi-pane session: only ONE row is the active pane of the
    # active window. The decoys come first, so any code that takes the first row
    # (or drops the '11' filter) picks a pane that belongs to no session.
    emit('0', '1', pane + '90')   # another window's active pane
    emit('1', '0', pane + '91')   # active window, inactive pane
    emit('1', '1', pane)          # the one we want
elif cmd == 'display-message':
    pane = pane_target(args[args.index('-t') + 1])
    fmt = args[-1] if args else ''
    name = session_of(pane)
    if fmt == '#{session_name}':
        print(name)
    elif fmt == '#{pane_current_path}':
        print(os.environ.get('TMUX_PANE_PATH', os.getcwd()))
    else:
        row = next((row for row in sessions if row[0] == name), None)
        print(row[1] if row and len(row) > 1 else '0')
elif cmd == 'capture-pane':
    pane_target(args[args.index('-t') + 1])
    # Default '> ' reads as "at the prompt". Override it to make detect_session_state
    # fall through its content checks to the IDLE-time branch, which is the only way
    # to observe whether the activity lookup actually found the session.
    print(os.environ.get('TMUX_PANE_CONTENT', '> '))
elif cmd == 'send-keys':
    pane_target(args[args.index('-t') + 1])
elif cmd == 'split-window':
    # Allocate a fresh pane id and remember it belongs to a live pane, so a later
    # send-keys/capture-pane targeting it resolves. Print only the id, as `-P -F`.
    existing = [row for row in panes if row[0].startswith('%split')]
    pane = '%split' + str(len(existing) + 1)
    with open(pstate, 'a') as stream:
        stream.write(pane + '|split-window-session\\n')
    print(pane)
elif cmd == 'new-session':
    rc = int(os.environ.get('TMUX_NEW_SESSION_RC', '0'))
    # TMUX_NEW_SESSION_DIES models the real hazard: tmux creates the session and
    # returns 0, then the supervised command dies on startup and tmux tears the
    # session down again. The session never becomes visible to later calls.
    register = rc == 0 and not os.environ.get('TMUX_NEW_SESSION_DIES')
    if register and '-s' in args:
        with open(state, 'a') as stream:
            stream.write(args[args.index('-s') + 1] + '|0|1\\n')
    raise SystemExit(rc)
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
    cwd: Path | None = None,
):
    return subprocess.run(
        [str(SHUTTLE), *args],
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        timeout=timeout,
        cwd=cwd,
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
    codex_home = Path(os.environ["CODEX_HOME"]).expanduser()
    if not codex_home.is_absolute():
        codex_home = Path.cwd() / codex_home
    codex_home = codex_home.resolve(strict=False)
    log_path = os.environ.get("FAKE_CODEX_HOME_LOG")
    if log_path:
        with open(log_path, "a", encoding="utf-8") as stream:
            stream.write(str(codex_home) + "\n")
    hooks_path = codex_home / "hooks.json"
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
        elif mode == "hook_errors_and_warnings":
            result = {
                "data": [
                    {
                        "cwd": cwd,
                        "hooks": [],
                        "warnings": [
                            "/tmp/broken-hooks.json: missing field `command`"
                        ],
                        "errors": [
                            {
                                "path": "/tmp/error-hooks.json",
                                "message": "bad hook",
                            }
                        ],
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


def test_go_accepts_a_brief_whose_first_heading_is_a_subheading(
    cli_env, tmp_path: Path
) -> None:
    # A '## Heading' body used to leave one '#', which scrubbed down to a leading
    # dash: the registry then saw "--title -whats-ready" and argparse read the
    # value as a flag, killing the launch under `set -e`.
    env, _ = cli_env
    bin_dir = Path(env["PATH"].split(":", 1)[0])
    _executable(
        bin_dir / "skein",
        "import sys\n"
        "if len(sys.argv) > 2 and sys.argv[1] == 'folio':\n"
        "    print('folio ' + sys.argv[2])\n"
        '    print("    ## Whats Ready")\n',
    )

    result = run_cli(env, "go", "-d", str(tmp_path), "brief-9")

    assert result.returncode == 0, result.stderr.decode()
    launch = Registry(env["SHUTTLE_HOME"]).list_launches()[-1]
    assert launch["title"] == "whats-ready"
    assert launch["tmux_session"] == "shuttle-whats-ready"


def test_dead_session_never_hands_off_to_a_prefix_neighbour(cli_env, tmp_path: Path) -> None:
    # The nastiest failure mode. `go` creates shuttle-provider-test, the supervised
    # command dies instantly, and a live session shares that name as a prefix.
    # tmux 3.0a's list-panes IGNORES the '=' exact prefix and slides to the
    # neighbour, so a session_pane() that trusted '=' would deliver the HANDOFF —
    # the user's brief — into somebody else's session.
    env, log = cli_env
    env["TMUX_SESSIONS"] = "shuttle-provider-test-neighbour|0|1"
    env["TMUX_NEW_SESSION_DIES"] = "1"

    result = run_cli(env, "go", "-d", str(tmp_path), "brief-1")

    assert result.returncode != 0, "go must fail loudly when its session died"
    sends = [call for call in tmux_calls(log) if call[0] == "send-keys"]
    assert sends == [], f"HANDOFF leaked into another session: {sends}"
    # And it must not read the neighbour's screen and announce a confident "Ready!"
    # for a session that is already dead. An honest timeout beats a lying signal.
    assert b"Ready!" not in result.stdout, result.stdout.decode()


@pytest.mark.parametrize("command", ["tail", "context"])
def test_tail_and_context_refuse_ambiguous_targets(cli_env, command: str) -> None:
    # Both used to pick a session with `tmux ls | grep "$TARGET" | head -1` — a
    # substring's FIRST hit, with no ambiguity check. They now go through the same
    # resolve_session funnel as peek/send/relay: exact wins, a partial must name
    # exactly one session, and ambiguity is an error rather than a silent guess.
    env, _ = cli_env
    env["TMUX_SESSIONS"] = "shuttle-task-one|0|1\nshuttle-task-two|0|1"

    result = run_cli(env, command, "shuttle-task", timeout=20)

    assert result.returncode == 2, result.stdout.decode()
    assert b"Ambiguous session target" in result.stderr


def test_export_does_not_let_a_session_name_steal_a_partial_uuid(
    cli_env, tmp_path: Path
) -> None:
    # export's branches are: numeric index, tmux session, then Claude UUID. Gating the
    # session branch on resolve_session (SUBSTRING matching) let any live session whose
    # name merely CONTAINED a uuid fragment swallow it, exporting the wrong transcript.
    # tmux matches by PREFIX, which is what this branch has always meant.
    env, _ = cli_env
    env["TMUX_SESSIONS"] = "shuttle-deadbee-decoy|0|1"

    history = Path(env["HOME"]) / ".claude" / "projects" / "-tmp-proj"
    history.mkdir(parents=True)
    transcript = history / "deadbee-1111-2222-3333.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "user",
                "message": {"role": "user", "content": "the real transcript"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    # A PARTIAL uuid — 'deadbee' is a substring of the decoy session's name, which is
    # exactly how the decoy stole it. It is not a PREFIX of it, so tmux won't match.
    result = run_cli(env, "export", "deadbee", timeout=30)

    assert result.returncode == 0, result.stderr.decode()
    assert b"the real transcript" in result.stdout, result.stdout.decode()


def test_send_survives_a_suffix_colliding_session_name(cli_env) -> None:
    # The activity lookup used `grep -F "$SESSION|"`, which matches any session whose
    # name ENDS with the target (the '|' delimiter follows the name). With 'zzr3a' and
    # 'xzzr3a' both live, two lines came back and `IDLE=$((NOW - ACTIVITY))` died with a
    # raw bash syntax error. Deterministic, not a race: the target always matches itself.
    env, log = cli_env
    env["TMUX_SESSIONS"] = "xshuttle-send|0|1\nshuttle-send|0|1"

    result = run_cli(env, "send", "--force", "shuttle-send", "probe")

    assert result.returncode == 0, result.stderr.decode()
    assert b"syntax error" not in result.stderr
    literal = next(call for call in tmux_calls(log) if call[0] == "send-keys" and "-l" in call)
    # '%2' is shuttle-send: the exact session, not its suffix-colliding neighbour.
    assert literal[literal.index("-t") + 1] == "%2"


@pytest.mark.parametrize("command", ["peek", "context", "tail"])
def test_a_vanished_session_never_leaks_an_empty_tmux_target(
    cli_env, command: str
) -> None:
    # The session resolves, then dies before its pane is bound. tmux NEVER fails on an
    # empty `-t` — it binds the caller's own session — so any command that forwards an
    # unchecked pane id silently reads or writes the WRONG session. Every such call site
    # must refuse instead.
    env, log = cli_env
    env["TMUX_SESSIONS"] = "shuttle-gone|0|1\nshuttle-caller|0|1"
    env["TMUX_PANELESS"] = "shuttle-gone"
    env["TMUX_CURRENT_SESSION"] = "shuttle-caller"

    result = run_cli(env, command, "shuttle-gone", timeout=20)

    assert result.returncode != 0, result.stdout.decode()
    empty_targets = [
        call for call in tmux_calls(log)
        if "-t" in call and call[call.index("-t") + 1] == ""
    ]
    assert empty_targets == [], f"leaked an empty -t: {empty_targets}"


def test_a_session_named_like_a_pane_id_is_treated_as_a_session(cli_env) -> None:
    # tmux accepts '%12' as a legal SESSION name. A "looks like a pane id" shortcut
    # would send `shuttle send %12` into whichever session actually owns pane %12 —
    # someone else's Claude prompt.
    env, log = cli_env
    # '%1' is the pane id the stub assigns to the FIRST session, so a session named
    # '%1' is a decoy for the victim that really owns that pane.
    env["TMUX_SESSIONS"] = "shuttle-victim|0|1\n%1|0|1"

    result = run_cli(env, "send", "--force", "%1", "probe")

    assert result.returncode == 0, result.stderr.decode()
    literal = next(call for call in tmux_calls(log) if call[0] == "send-keys" and "-l" in call)
    # '%2' is the session actually NAMED '%1'. Sending to '%1' would hit the victim.
    assert literal[literal.index("-t") + 1] == "%2", "typed into the victim's pane"


def test_resume_survives_a_dash_leading_stored_title(cli_env, tmp_path: Path) -> None:
    # `go` slugs its title, but `resume` reads the title back from the REGISTRY, which
    # stores it raw. A stored '-foo' passed as `--title -foo` is read by argparse as a
    # flag, and the resume dies under `set -e`. The `--opt=value` form is what prevents it.
    env, _ = cli_env
    registry = Registry(env["SHUTTLE_HOME"])
    old = registry.create_launch(
        provider="codex",
        mode="go",
        cwd=str(tmp_path),
        tmux_session="shuttle-codex-old",
        pane_id="%9",
        pid=os.getpid(),
        title="-foo",
        native_session_id="0199aaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    )
    registry.close(old["launch_id"], status="exited", exit_code=0)

    result = run_cli(env, "resume", "0199aaaa-bbbb-cccc-dddd-eeeeeeeeeeee", timeout=30)

    assert b"expected one argument" not in result.stderr, result.stderr.decode()
    assert result.returncode == 0, result.stderr.decode()


def _skein_finds_nothing(env: dict[str, str]) -> None:
    # Make `skein folio` print nothing, so `go`/`split` treat the brief as not found.
    bin_dir = Path(env["PATH"].split(":", 1)[0])
    _executable(bin_dir / "skein", "import sys\n")


def test_go_sanitizes_a_raw_brief_id_into_the_session_name(cli_env, tmp_path: Path) -> None:
    # With -d explicit and the brief not found, `go` builds the session name straight
    # from the RAW brief id. tmux rejects '.'/':' and mis-parses '|'/newline/space, so
    # the id must be scrubbed — and the --brief=value registry arg must still carry the
    # raw id (a leading '-' would be read as a flag in the space-separated form).
    env, _ = cli_env
    _skein_finds_nothing(env)

    result = run_cli(env, "go", "-d", str(tmp_path), "-weird.brief:id", timeout=30)

    assert result.returncode == 0, result.stderr.decode()
    launch = Registry(env["SHUTTLE_HOME"]).list_launches()[-1]
    assert launch["tmux_session"] == "shuttle-weird-brief-id"
    assert launch["brief"] == "-weird.brief:id", "raw brief id was not carried through"


def test_go_never_collapses_two_briefs_onto_one_session(cli_env, tmp_path: Path) -> None:
    # Two all-punctuation brief ids both scrub to empty. Without a non-empty fallback
    # they collapse to the same 'shuttle-' name, so the second `go` reboards the FIRST
    # brief's session instead of launching its own.
    env, _ = cli_env
    _skein_finds_nothing(env)

    run_cli(env, "go", "-d", str(tmp_path), "!!!", timeout=30)
    run_cli(env, "go", "-d", str(tmp_path), "@@@", timeout=30)

    names = {r["tmux_session"] for r in Registry(env["SHUTTLE_HOME"]).list_launches()}
    assert "shuttle-" not in names, "an empty session name leaked through"
    assert len(names) == 2, f"two distinct briefs collapsed onto one session: {names}"


def test_split_delivers_handoff_to_the_new_pane_by_id(cli_env, tmp_path: Path) -> None:
    # split creates a pane and must hand the id straight to tmux_send_pane. Sending by
    # session name instead (tmux_send_cmd) makes the HANDOFF undeliverable — tmux would
    # look for a *session* named '%split1' and find none.
    env, log = cli_env
    env["TMUX"] = "/tmp/fake-tmux,1,0"

    result = run_cli(env, "split", "-d", str(tmp_path), "brief-1", timeout=30)

    assert result.returncode == 0, result.stderr.decode()
    literal = next(
        call for call in tmux_calls(log) if call[0] == "send-keys" and "-l" in call
    )
    target = literal[literal.index("-t") + 1]
    assert target.startswith("%split"), f"HANDOFF not sent to the new pane: {target}"
    assert literal[-2:] == ["--", "HANDOFF: brief-1"]


def test_status_handles_a_pipe_in_a_session_name(cli_env) -> None:
    # tmux permits '|' in a session name. Parsing list-sessions on '|' truncated NAME,
    # so provider_for_session looked up the wrong session, the launch was printed a
    # second time as a phantom stale row, and ATTACHED got a fragment of the name —
    # which could abort a bare `shuttle` under `set -e` in the arithmetic below.
    env, _ = cli_env
    env["TMUX_SESSIONS_JSON"] = json.dumps([["shuttle-pipe|x", "0", "1"]])

    result = run_cli(env, "status", timeout=30)

    assert result.returncode == 0, result.stderr.decode()
    assert b"integer expression expected" not in result.stderr
    assert b"shuttle-pipe|x" in result.stdout, result.stdout.decode()


def test_status_survives_the_separator_byte_inside_a_foreign_session_name(cli_env) -> None:
    # Whatever separator status picks, tmux permits it in a session name. A foreign
    # session whose name contains the 0x1f separator itself splits into junk fields, so
    # ACTIVITY is non-numeric — the arithmetic guard must catch it rather than letting
    # `$((NOW - junk))` abort a bare `shuttle` under `set -e`.
    env, _ = cli_env
    env["TMUX_SESSIONS_JSON"] = json.dumps([["zzr-usx", "0", "1"]])

    result = run_cli(env, "status", timeout=30)

    assert result.returncode == 0, result.stderr.decode()
    # No arithmetic crash (ACTIVITY guard) and no `[: x: integer expression expected`
    # noise from a non-numeric ATTACHED (ATTACHED guard).
    assert b"syntax error" not in result.stderr
    assert b"arithmetic" not in result.stderr
    assert b"expected" not in result.stderr, result.stderr.decode()


@pytest.mark.parametrize("name", ["shuttle-back\\x", "shuttle-sp ace"])
def test_pane_lookup_survives_awkward_session_names(cli_env, name: str) -> None:
    # `awk -v want=...` processes escape sequences, so a session named 'shuttle-back\x'
    # arrived at awk mangled ('\x' is a hex escape in gawk) and never compared equal to
    # itself — every pane-facing command (peek/send/relay/tail/status) funnels through
    # session_pane, so the session became completely unreachable. ENVIRON does no such
    # processing.
    env, log = cli_env
    env["TMUX_SESSIONS_JSON"] = json.dumps([[name, "0", "1"]])

    result = run_cli(env, "peek", name, timeout=20)

    assert result.returncode == 0, result.stderr.decode()
    capture = next(call for call in tmux_calls(log) if call[0] == "capture-pane")
    assert capture[capture.index("-t") + 1] == "%1"


def test_idle_time_uses_an_exact_activity_lookup(cli_env) -> None:
    # session_activity() must find the session by its EXACT name. `awk -v want=...`
    # processes escape sequences, so a name containing a backslash never matched and the
    # lookup returned nothing — idle then computed as NOW-0, i.e. decades, and the
    # session was reported 'stuck' when it was merely quiet. ENVIRON does no escaping.
    env, _ = cli_env
    now = int(time.time())
    env["TMUX_SESSIONS_JSON"] = json.dumps([["shuttle-back\\x", "0", str(now - 5)]])
    env["TMUX_PANE_CONTENT"] = "nothing conclusive on this screen"

    result = run_cli(env, "send", "shuttle-back\\x", "hi", timeout=20)

    output = (result.stdout + result.stderr).decode()
    assert result.returncode == 1, output
    # Five seconds idle: 'unknown' state. A failed lookup would read as decades -> 'stuck'.
    assert "unknown" in output, output
    assert "stuck" not in output, output


def test_ground_kills_only_exact_session_names(cli_env) -> None:
    # A bare kill target prefix-matches. If a session exits between the listing and
    # the kill, tmux would kill a NEIGHBOUR whose name starts with it.
    env, log = cli_env
    env["TMUX_SESSIONS"] = "shuttle-foo|0|1\nshuttle-foo-long|0|1"

    result = run_cli(env, "ground")

    assert result.returncode == 0, result.stderr.decode()
    kills = [call for call in tmux_calls(log) if call[0] == "kill-session"]
    assert kills, "ground killed nothing"
    for call in kills:
        target = call[call.index("-t") + 1]
        assert target.startswith("="), f"inexact kill target: {target}"


def test_pane_passthrough_only_accepts_real_pane_ids(cli_env) -> None:
    # tmux permits a SESSION named '%foo'. Forwarding that as though it were a pane
    # id would target nothing, and the command would exit 0 having captured nothing.
    env, log = cli_env
    env["TMUX_SESSIONS"] = "%weird-name|0|1"

    result = run_cli(env, "peek", "%weird-name")

    assert result.returncode == 0, result.stderr.decode()
    capture = next(call for call in tmux_calls(log) if call[0] == "capture-pane")
    assert capture[capture.index("-t") + 1] == "%1"


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
    capture = next(call for call in tmux_calls(log) if call[0] == "capture-pane")
    # The exact session 'task' must win over the partial matches, and it must be
    # addressed by pane id — "=task" is not a legal pane target on tmux 3.0a.
    listing = next(call for call in tmux_calls(log) if call[0] == "list-panes")
    assert listing[listing.index("-t") + 1] == "=task"
    assert capture[capture.index("-t") + 1] == "%1"

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
    [("board", "attach"), ("kill", "kill-session")],
)
def test_session_target_commands_use_exact_name(
    cli_env, command: str, tmux_command: str
) -> None:
    env, log = cli_env
    env["TMUX_SESSIONS"] = "shuttle-unique-name|0|1"
    result = run_cli(env, command, "unique")
    assert result.returncode == 0, result.stderr.decode()
    call = next(call for call in tmux_calls(log) if call[0] == tmux_command)
    assert call[call.index("-t") + 1] == "=shuttle-unique-name"


@pytest.mark.parametrize(
    ("argv", "tmux_command"),
    [
        (("peek", "unique"), "capture-pane"),
        (("send", "--force", "unique", "hello"), "send-keys"),
    ],
)
def test_pane_target_commands_use_a_pane_id_never_an_equals_name(
    cli_env, argv: tuple[str, ...], tmux_command: str
) -> None:
    # tmux 3.0a rejects "=name" on pane targets, so these must resolve to a pane id.
    env, log = cli_env
    env["TMUX_SESSIONS"] = "shuttle-unique-name|0|1"
    result = run_cli(env, *argv)
    assert result.returncode == 0, result.stderr.decode()
    call = next(call for call in tmux_calls(log) if call[0] == tmux_command)
    target = call[call.index("-t") + 1]
    assert target.startswith("%"), target


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
    assert literal[literal.index("-t") + 1].startswith("%")
    assert literal[-2:] == ["--", "hello from relay"]


def test_hooks_snippet_and_doctor_never_write_dotfiles(cli_env) -> None:
    env, _ = cli_env
    hooks = Path(env["HOME"]) / ".codex" / "hooks.json"
    snippet = run_cli(env, "hooks")
    assert snippet.returncode == 0
    assert b"not a replacement" in snippet.stdout
    assert b"CODEX_HOME/hooks.json or ~/.codex/hooks.json" in snippet.stdout
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


def test_hooks_install_and_doctor_honor_codex_home_env(cli_env, tmp_path: Path) -> None:
    env, _ = cli_env
    install_fake_codex_app_server(env)
    user_home = Path(env["HOME"])
    default_codex_dir = user_home / ".codex"
    codex_home = tmp_path / "custom-codex-home"
    hooks = codex_home / "hooks.json"
    config = codex_home / "config.toml"
    probe_log = tmp_path / "codex-home.log"
    env["CODEX_HOME"] = str(codex_home)
    env["FAKE_CODEX_HOME_LOG"] = str(probe_log)

    installed = run_cli(env, "hooks", "install")

    assert installed.returncode == 0, installed.stderr.decode()
    assert f"updated: {hooks}".encode() in installed.stdout
    assert hooks.exists()
    assert not default_codex_dir.exists()
    assert not config.exists()

    doctor = run_cli(env, "hooks", "doctor")

    assert doctor.returncode == 0, doctor.stderr.decode()
    assert f"configured: {hooks}".encode() in doctor.stdout
    assert b"all Shuttle Codex hooks are trusted and enabled" in doctor.stdout
    assert probe_log.read_text(encoding="utf-8").splitlines() == [str(codex_home)]
    assert not default_codex_dir.exists()
    assert not config.exists()


def test_hooks_install_and_doctor_default_codex_home_without_env(
    cli_env, tmp_path: Path
) -> None:
    env, _ = cli_env
    install_fake_codex_app_server(env)
    env.pop("CODEX_HOME", None)
    codex_home = Path(env["HOME"]) / ".codex"
    hooks = codex_home / "hooks.json"
    probe_log = tmp_path / "codex-home.log"
    env["FAKE_CODEX_HOME_LOG"] = str(probe_log)

    installed = run_cli(env, "hooks", "install")

    assert installed.returncode == 0, installed.stderr.decode()
    assert f"updated: {hooks}".encode() in installed.stdout
    assert hooks.exists()

    doctor = run_cli(env, "hooks", "doctor")

    assert doctor.returncode == 0, doctor.stderr.decode()
    assert f"configured: {hooks}".encode() in doctor.stdout
    assert b"all Shuttle Codex hooks are trusted and enabled" in doctor.stdout
    assert probe_log.read_text(encoding="utf-8").splitlines() == [str(codex_home)]


def test_hooks_install_and_doctor_resolve_relative_codex_home_against_subprocess_cwd(
    cli_env, tmp_path: Path
) -> None:
    env, _ = cli_env
    install_fake_codex_app_server(env)
    cwd = tmp_path / "subprocess-cwd"
    cwd.mkdir()
    env["CODEX_HOME"] = "relative-codex-home"
    expected_codex_home = cwd / "relative-codex-home"
    hooks = expected_codex_home / "hooks.json"
    config = expected_codex_home / "config.toml"
    default_codex_dir = Path(env["HOME"]) / ".codex"
    probe_log = tmp_path / "codex-home.log"
    env["FAKE_CODEX_HOME_LOG"] = str(probe_log)

    installed = run_cli(env, "hooks", "install", cwd=cwd)

    assert installed.returncode == 0, installed.stderr.decode()
    assert f"updated: {hooks}".encode() in installed.stdout
    assert hooks.exists()
    assert not default_codex_dir.exists()
    assert not config.exists()

    doctor = run_cli(env, "hooks", "doctor", cwd=cwd)

    assert doctor.returncode == 0, doctor.stderr.decode()
    assert f"configured: {hooks}".encode() in doctor.stdout
    assert b"all Shuttle Codex hooks are trusted and enabled" in doctor.stdout
    assert probe_log.read_text(encoding="utf-8").splitlines() == [
        str(expected_codex_home)
    ]
    assert not default_codex_dir.exists()
    assert not config.exists()


def test_hooks_explicit_home_ignores_codex_home_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    explicit_home = tmp_path / "explicit-home"
    env_codex_home = tmp_path / "env-codex-home"
    hooks = explicit_home / ".codex" / "hooks.json"
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(env_codex_home))

    installed = shuttle_cli.install_hooks(explicit_home)

    assert installed.path == hooks
    assert hooks.exists()
    assert not (env_codex_home / "hooks.json").exists()

    captured_probe_homes: list[Path] = []

    def trust_probe(home: Path, probe_cwd: Path) -> shuttle_cli.HookTrustProbeResult:
        captured_probe_homes.append(home)
        metadata = []
        for event in HOOK_EVENTS:
            metadata.append(
                {
                    "key": (
                        f"{hooks}:{shuttle_cli._codex_event_key(event)}:0:0"
                    ),
                    "source": "user",
                    "sourcePath": str(hooks),
                    "eventName": shuttle_cli._codex_event_name(event),
                    "handlerType": "command",
                    "command": HOOK_COMMAND["command"],
                    "currentHash": "sha256:test",
                    "enabled": True,
                    "trustStatus": "trusted",
                }
            )
        return shuttle_cli.HookTrustProbeResult(
            data=[
                {
                    "cwd": str(probe_cwd),
                    "hooks": metadata,
                    "warnings": [],
                    "errors": [],
                }
            ]
        )

    healthy, messages = shuttle_cli.hooks_diagnostics(
        explicit_home, trust_probe=trust_probe, cwd=cwd
    )

    assert healthy
    assert captured_probe_homes == [explicit_home]
    assert f"configured: {hooks}" in messages
    assert "all Shuttle Codex hooks are trusted and enabled" in messages
    assert not (env_codex_home / "hooks.json").exists()


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
        "description": "  Existing Codex hook description stays exact.  ",
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
    }
    write_json(hooks, existing)
    hooks.chmod(0o640)
    original_bytes = hooks.read_bytes()
    before_stat = hooks.stat()

    result = run_cli(env, "hooks", "install")

    assert result.returncode == 0, result.stderr.decode()
    assert b"updated:" in result.stdout
    installed = json.loads(hooks.read_text(encoding="utf-8"))
    assert installed["description"] == existing["description"]
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


@pytest.mark.parametrize("async_value", [True, False])
def test_hooks_install_preserves_valid_async_values(
    cli_env, async_value: bool
) -> None:
    env, _ = cli_env
    codex_dir = Path(env["HOME"]) / ".codex"
    codex_dir.mkdir()
    hooks = codex_dir / "hooks.json"
    config = codex_dir / "config.toml"
    existing_hook = {
        "type": "command",
        "command": "echo keep",
        "async": async_value,
    }
    existing = {"hooks": {"SessionStart": [{"hooks": [existing_hook]}]}}
    write_json(hooks, existing)
    hooks.chmod(0o640)
    original_bytes = hooks.read_bytes()
    config.write_text("sentinel = true\n", encoding="utf-8")

    result = run_cli(env, "hooks", "install")

    assert result.returncode == 0, result.stderr.decode()
    installed = json.loads(hooks.read_text(encoding="utf-8"))
    assert installed["hooks"]["SessionStart"][0]["hooks"] == [
        existing_hook,
        HOOK_COMMAND,
    ]
    for event in ("UserPromptSubmit", "PermissionRequest", "Stop"):
        assert hook_coords(installed, event) == [(0, 0)]
    assert S_IMODE(hooks.stat().st_mode) == 0o640
    assert config.read_text(encoding="utf-8") == "sentinel = true\n"
    backups = list(codex_dir.glob("hooks.json.shuttle.*.bak"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original_bytes
    assert not list(codex_dir.glob(".hooks.json.*.tmp"))


@pytest.mark.parametrize(
    "description",
    [
        "  Existing Codex hook description stays exact.  ",
        None,
    ],
)
def test_hooks_install_preserves_valid_description_values(
    cli_env, description: str | None
) -> None:
    env, _ = cli_env
    codex_dir = Path(env["HOME"]) / ".codex"
    codex_dir.mkdir()
    hooks = codex_dir / "hooks.json"
    existing = {"description": description, "hooks": {}}
    write_json(hooks, existing)

    result = run_cli(env, "hooks", "install")

    assert result.returncode == 0, result.stderr.decode()
    installed = json.loads(hooks.read_text(encoding="utf-8"))
    assert installed["description"] == description
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


def test_hooks_doctor_reports_app_server_hook_errors_and_warnings(cli_env) -> None:
    env, _ = cli_env
    install_fake_codex_app_server(env)
    env["FAKE_CODEX_APP_SERVER_MODE"] = "hook_errors_and_warnings"
    codex_dir = Path(env["HOME"]) / ".codex"
    codex_dir.mkdir()
    hooks = codex_dir / "hooks.json"
    write_json(
        hooks, {"hooks": {event: [{"hooks": [HOOK_COMMAND]}] for event in HOOK_EVENTS}}
    )

    result = run_cli(env, "hooks", "doctor")

    assert result.returncode == 1
    assert b"trust unverified: Codex hooks/list returned hook errors" in result.stdout
    assert b"hook error: /tmp/error-hooks.json: bad hook" in result.stdout
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


def test_hooks_doctor_rejects_explicit_null_hooks(cli_env) -> None:
    env, _ = cli_env
    codex_dir = Path(env["HOME"]) / ".codex"
    codex_dir.mkdir()
    hooks = codex_dir / "hooks.json"
    body = b'{"hooks":null}\n'
    hooks.write_bytes(body)

    result = run_cli(env, "hooks", "doctor")

    assert result.returncode == 1
    assert b"top-level 'hooks' must be an object" in result.stdout
    assert hooks.read_bytes() == body
    assert not list(codex_dir.glob("hooks.json.shuttle.*.bak"))
    assert not list(codex_dir.glob(".hooks.json.*.tmp"))


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


def test_hooks_install_refuses_unknown_top_level_key_before_backup(cli_env) -> None:
    env, _ = cli_env
    codex_dir = Path(env["HOME"]) / ".codex"
    codex_dir.mkdir()
    hooks = codex_dir / "hooks.json"
    config = codex_dir / "config.toml"
    body = (
        b'{"description":"keep","metadata":{"unsupported":true},'
        b'"hooks":{"SessionStart":[]}}\n'
    )
    hooks.write_bytes(body)
    hooks.chmod(0o640)
    config.write_text("sentinel = true\n", encoding="utf-8")

    result = run_cli(env, "hooks", "install")

    assert result.returncode == 1
    assert b"top-level key 'metadata' is not supported" in result.stderr
    assert b"allowed keys: description, hooks" in result.stderr
    assert hooks.read_bytes() == body
    assert S_IMODE(hooks.stat().st_mode) == 0o640
    assert config.read_text(encoding="utf-8") == "sentinel = true\n"
    assert not list(codex_dir.glob("hooks.json.shuttle.*.bak"))
    assert not list(codex_dir.glob(".hooks.json.*.tmp"))


@pytest.mark.parametrize(
    ("document", "needle"),
    [
        ({"hooks": None}, b"top-level 'hooks' must be an object"),
        (
            {"description": 7, "hooks": {}},
            b"top-level 'description' must be a string or null",
        ),
        (
            {"description": True, "hooks": {}},
            b"top-level 'description' must be a string or null",
        ),
        (
            {"description": ["no"], "hooks": {}},
            b"top-level 'description' must be a string or null",
        ),
        (
            {"description": {"no": True}, "hooks": {}},
            b"top-level 'description' must be a string or null",
        ),
    ],
)
def test_hooks_install_refuses_invalid_top_level_values_before_backup(
    cli_env, document: dict, needle: bytes
) -> None:
    env, _ = cli_env
    codex_dir = Path(env["HOME"]) / ".codex"
    codex_dir.mkdir()
    hooks = codex_dir / "hooks.json"
    config = codex_dir / "config.toml"
    body = (json.dumps(document, separators=(",", ":")) + "\n").encode()
    hooks.write_bytes(body)
    hooks.chmod(0o640)
    config.write_text("sentinel = true\n", encoding="utf-8")

    result = run_cli(env, "hooks", "install")

    assert result.returncode == 1
    assert result.stderr.startswith(b"shuttle hooks:")
    assert needle in result.stderr
    assert b"Traceback" not in result.stderr
    assert hooks.read_bytes() == body
    assert S_IMODE(hooks.stat().st_mode) == 0o640
    assert config.read_text(encoding="utf-8") == "sentinel = true\n"
    assert not list(codex_dir.glob("hooks.json.shuttle.*.bak"))
    assert not list(codex_dir.glob(".hooks.json.*.tmp"))


@pytest.mark.parametrize(
    "async_value",
    [
        None,
        0,
        1,
        "false",
        [],
        {"enabled": True},
    ],
)
def test_hooks_install_refuses_invalid_async_values_before_backup(
    cli_env, async_value: object
) -> None:
    env, _ = cli_env
    codex_dir = Path(env["HOME"]) / ".codex"
    codex_dir.mkdir()
    hooks = codex_dir / "hooks.json"
    config = codex_dir / "config.toml"
    document = {
        "hooks": {
            "SessionStart": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "echo keep",
                            "async": async_value,
                        }
                    ]
                }
            ]
        }
    }
    body = (json.dumps(document, separators=(",", ":")) + "\n").encode()
    hooks.write_bytes(body)
    hooks.chmod(0o640)
    config.write_text("sentinel = true\n", encoding="utf-8")

    result = run_cli(env, "hooks", "install")

    assert result.returncode == 1
    assert result.stderr == (
        b"shuttle hooks: "
        b"hooks.SessionStart[0].hooks[0].async must be a boolean\n"
    )
    assert hooks.read_bytes() == body
    assert S_IMODE(hooks.stat().st_mode) == 0o640
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
