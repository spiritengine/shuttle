# Shuttle

CLI for launching and managing Claude Code pairing sessions.

## Commands

```bash
shuttle              # status with state indicators (⏳🧠🔒💤⚠️)
shuttle watch        # continuous status refresh (Ctrl-C to exit)
shuttle go <brief>   # launch session on brief (new window)
shuttle split <brief>   # open brief in horizontal split pane
shuttle vsplit <brief>  # open brief in vertical split pane
shuttle unsplit         # close split pane
shuttle board <n>    # attach to session
shuttle peek <n>     # show last 20 lines from session
shuttle tail <n>     # live output stream (Ctrl-C to exit)
shuttle send <n> <msg>  # send message to session
shuttle relay <n> <file>  # send file contents to session
shuttle context <n>  # show conversation context from CC history
shuttle whoami       # print unique probe for session identification
shuttle confirm <p>  # find session ID containing probe
shuttle index        # build session index for fast search
shuttle index -u     # incremental update (fast, ~8s)
shuttle search -l -a # list all sessions from index
shuttle search <q>   # search content (uses index)
shuttle search -l -s 7  # list sessions from last 7 days
shuttle resume <id>  # resume past session by UUID (from search)
shuttle export <n|id>  # export session to markdown or JSON
shuttle ls           # list sessions
shuttle kill <n>     # kill session
shuttle ground       # kill all shuttle sessions
shuttle config       # show current configuration
shuttle doctor       # diagnose gnome-terminal launch environment
```

## Session State Indicators

Status shows each session's state:
- ⏳ running  - Tool is executing (waiting for output)
- 🧠 thinking - Claude is processing/generating response
- 🔒 approval - Waiting for user to approve an action
- 💤 waiting  - At prompt, waiting for input
- ⚠️ stuck    - Idle >5min with unclear state
- ○ unknown  - Cannot determine state

## Project Directory Options

For `go`, `split`, and `vsplit`:

```bash
shuttle go -p skein brief-20251210-ceor  # launch in ~/projects/skein
shuttle go -d /path/to/project brief-id  # launch in specified directory
shuttle split -p spindle brief-id        # split in spindle project
```

Default: current working directory.

## Headless Mode

For SSH/server use without a display:

```bash
shuttle go --headless brief-id  # create session, print attach command
shuttle board --headless 1      # attach in current terminal
SHUTTLE_HEADLESS=1 shuttle go brief-id  # env var override
```

Auto-detects: if `$DISPLAY` is unset, headless mode is automatic.

## Sending Messages to Sessions

Send text to a running session's Claude prompt:

```bash
shuttle send 1 "check the failing tests"
shuttle send 2 "skein torch"  # trigger retirement
shuttle send myproject "look at src/auth.py"
```

The `send` command checks session state before sending:
- If Claude is working/processing, it warns and refuses (use `--force` to override)
- If Claude is in approval mode, it warns and refuses
- Only sends when Claude is waiting at the prompt

Options:
- `--wait, -w` - Wait for session to reach prompt (up to 30s)
- `--force, -f` - Send regardless of session state

```bash
shuttle send --wait 1 "message"   # wait for prompt, then send
shuttle send --force 1 "urgent"   # send immediately regardless of state
```

Useful for nudging agents, giving directions, or triggering retirement.

## For Agents

You can use shuttle to launch pairing sessions for humans:

```bash
# Launch a session on a brief so human can pair
shuttle go brief-20251210-ceor
```

This opens a new terminal with CC already ignited on the brief.

## Configuration

Shuttle reads settings from `~/.config/shuttle/config` or `~/.shuttlerc`:

```bash
# Default project directory (empty = use pwd)
SHUTTLE_DEFAULT_PROJECT=~/projects/main

# Default SKEIN site for briefs
SHUTTLE_DEFAULT_SITE=ledger

# Always run headless (set to 1, or leave empty for auto-detect)
SHUTTLE_HEADLESS_DEFAULT=

# Prompt detection timeout in seconds
SHUTTLE_PROMPT_TIMEOUT=12
```

Precedence: CLI flags > Environment variables > Config file > Defaults

Run `shuttle config` to see current settings.

Shuttle launches windows via gnome-terminal. There is no terminal-selection
config knob — gnome-terminal is the target. If gnome-terminal-server is in a
bad D-Bus state, shuttle falls back to xterm for that one launch so the session
stays reachable; it will not restart gnome-terminal-server automatically
(that would kill other shuttle sessions). Run `shuttle doctor` to diagnose.

## Session Index

Shuttle maintains an index of all CC sessions for fast search. The index stores:
- Session UUID, project path, file location
- Started/last activity timestamps
- Message count and first user message (summary)

```bash
shuttle index             # full rebuild (~3 min for 1000+ sessions)
shuttle index --update    # incremental update (~8s)
```

Index location: `~/.shuttle/index.json`

Search uses the index for filtering, then greps matching files:

```bash
shuttle search "auth" --all        # content search across all projects
shuttle search --list --since 7    # list sessions from last 7 days
shuttle search -l -p speakbot      # list sessions for speakbot project
```

## Architecture

- Single bash script at `bin/shuttle`
- Uses tmux for session management
- Uses configurable terminal emulator (default: gnome-terminal)
- Integrates with SKEIN for briefs and shards
- Session index in `~/.shuttle/index.json` for fast search
