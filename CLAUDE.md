# Shuttle

CLI for launching and managing Claude Code pairing sessions.

## Commands

```bash
shuttle              # status with state indicators (⚡🔒💤⚠️)
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
shuttle search <q>   # search CC session history (--all for all projects)
shuttle resume <id>  # resume past session by UUID (from search)
shuttle export <n|id>  # export session to markdown or JSON
shuttle ls           # list sessions
shuttle kill <n>     # kill session
shuttle ground       # kill all shuttle sessions
shuttle config       # show current configuration
```

## Session State Indicators

Status shows each session's state:
- ⚡ working  - Claude is actively processing
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

# Terminal emulator command
SHUTTLE_TERMINAL="gnome-terminal --"

# Always run headless (set to 1, or leave empty for auto-detect)
SHUTTLE_HEADLESS_DEFAULT=

# Prompt detection timeout in seconds
SHUTTLE_PROMPT_TIMEOUT=12
```

Precedence: CLI flags > Environment variables > Config file > Defaults

Run `shuttle config` to see current settings.

## Architecture

- Single bash script at `bin/shuttle`
- Uses tmux for session management
- Uses configurable terminal emulator (default: gnome-terminal)
- Integrates with SKEIN for briefs and shards
