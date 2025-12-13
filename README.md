# Shuttle

CLI for managing Claude Code agent sessions. Launch, board, and manage CC sessions from the command line.

## Install

```bash
ln -sf ~/projects/shuttle/bin/shuttle ~/bin/shuttle
```

## Usage

```bash
shuttle                  # show status (sessions, briefs, shards)
shuttle go <brief-id>    # launch new session on a brief
shuttle board <n>        # board session by number or name
shuttle ls               # list active sessions
shuttle kill <n>         # kill session by number or name
shuttle ground           # kill all shuttle sessions
shuttle briefs [site]    # show open briefs
```

## Examples

```bash
# Launch a session on a brief
shuttle go brief-20251210-ceor

# Board the first session
shuttle board 1

# Board by partial name match
shuttle board diverge

# Kill session #2
shuttle kill 2

# Ground all shuttle sessions
shuttle ground
```

## How it works

- Creates tmux sessions with Claude Code
- Auto-sends `HANDOFF: <brief-id>` to ignite on the brief
- Names sessions based on brief titles for easy identification
- Reboards existing sessions instead of creating duplicates

## Related

- **SKEIN** - Knowledge management, briefs, folios
- **Spindle** - CC-to-CC delegation, async agent spawning
- **Horizon** - API server, mantles, cast
- **Mill** - Scheduled/autonomous work
