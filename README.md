# Shuttle

CLI for managing Claude Code and Codex sessions. Launch, board, and manage both providers from the command line.

## Install

```bash
ln -sf ~/projects/shuttle/bin/shuttle ~/bin/shuttle
```

## Usage

```bash
shuttle                  # show status (sessions, briefs, shards)
shuttle go <brief-id>    # launch new session on a brief
shuttle go --agent codex <brief-id>  # launch Codex (Claude remains default)
shuttle board <n>        # board session by number or name
shuttle ls               # list active sessions
shuttle kill <n>         # kill session by number or name
shuttle ground           # kill all shuttle sessions
```

## Examples

```bash
# Launch a session on a brief
shuttle go brief-20251210-ceor

# Board the first session
shuttle board 1

# Board by an unambiguous partial name match
shuttle board diverge

# Kill session #2
shuttle kill 2

# Ground all shuttle sessions
shuttle ground
```

## How it works

- Creates supervised tmux sessions with provider-neutral registry records
- Sends Claude `HANDOFF: <brief-id>` after readiness; passes it to Codex as the initial prompt
- Names sessions based on brief titles for easy identification
- Uses `shuttle-<title>` for Claude and `shuttle-codex-<title>` for Codex
- Reboards existing same-provider sessions instead of creating duplicates
- Uses structured Codex hook state for status and safe send decisions

Codex hooks are never installed automatically. `shuttle hooks install` additively
merges Shuttle's command hook into `~/.codex/hooks.json` without replacing other
hooks, and `shuttle hooks doctor` diagnoses the file and Codex trust records
without writing either one. Codex still launches with a clear degraded warning
when hooks are absent or untrusted.

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

# Quick import test
python3 -c "from parser import load; s = load('test.jsonl'); print(s.total_tokens())"

# Full test suite (requires session files)
# Copy from compress repo for full tests:
# cp ~/projects/spiritengine/compress/sessions/*.jsonl sessions/
pytest parser/test_parser.py -v
# 74 tests total (64 pass without session files, 74 with)
```

## Related

- **SKEIN** - Knowledge management, briefs, folios
- **Spindle** - CC-to-CC delegation, async agent spawning
- **Horizon** - API server, mantles, cast
- **Mill** - Scheduled/autonomous work
