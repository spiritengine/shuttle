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

## Parser

Shuttle uses the lazarus parser from spiritengine/compress for robust JSONL session parsing.

**Location:** `parser/` (vendored copy)
**Source:** `~/projects/spiritengine/compress/parser/`
**Last synced:** 2026-01-30
**Tests:** 88 tests, property-based testing

The parser provides:
- Round-trip faithful JSONL parsing
- Type-safe models (Event, Message, ToolPair, Session)
- Token counting, message reconstruction
- Tool use/result matching

**Usage:**
```python
from parser import load, Session

session = Session.from_path("session.jsonl")
print(f"{len(session.events)} events, {session.total_tokens()} tokens")

for pair in session.tool_pairs():
    print(f"{pair.tool_name}: {pair.result_size} chars")
```

**Testing:**
```bash
cd ~/projects/shuttle
python3 -c "from parser import load; s = load('test.jsonl'); print(s.total_tokens())"
```

## Related

- **SKEIN** - Knowledge management, briefs, folios
- **Spindle** - CC-to-CC delegation, async agent spawning
- **Horizon** - API server, mantles, cast
- **Mill** - Scheduled/autonomous work
