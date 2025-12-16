# Shuttle

CLI for launching and managing Claude Code pairing sessions.

## Commands

```bash
shuttle              # status: sessions, briefs, shards
shuttle watch        # continuous status refresh (Ctrl-C to exit)
shuttle go <brief>   # launch session on brief (new window)
shuttle split <brief>   # open brief in horizontal split pane
shuttle vsplit <brief>  # open brief in vertical split pane
shuttle unsplit         # close split pane
shuttle board <n>    # attach to session
shuttle send <n> <msg>  # send message to session
shuttle ls           # list sessions
shuttle kill <n>     # kill session
shuttle ground       # kill all shuttle sessions
shuttle briefs       # show open briefs
```

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

## Architecture

- Single bash script at `bin/shuttle`
- Uses tmux for session management
- Uses gnome-terminal for new windows
- Integrates with SKEIN for briefs and shards
