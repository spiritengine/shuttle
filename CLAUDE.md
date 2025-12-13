# Shuttle

CLI for launching and managing Claude Code pairing sessions.

## Commands

```bash
shuttle              # status: sessions, briefs, shards
shuttle go <brief>   # launch session on brief (new window)
shuttle split <brief>   # open brief in horizontal split pane
shuttle vsplit <brief>  # open brief in vertical split pane
shuttle unsplit         # close split pane
shuttle board <n>    # attach to session
shuttle ls           # list sessions
shuttle kill <n>     # kill session
shuttle ground       # kill all shuttle sessions
shuttle briefs       # show open briefs
```

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
