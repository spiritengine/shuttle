# Sample Shuttle Export Format

This document shows the proposed markdown format for `shuttle export`.

---

# Session: e990002f-2b59-47cb-8101-47ac6e730f3f
**Project:** shuttle
**Directory:** /home/patrick/projects/shuttle
**Branch:** master
**Date:** 2025-12-15

---

## 👤 User

HANDOFF: brief-20251215-eyum

## 🤖 Claude

Let me check what briefs are available:

## 🤖 Claude

The brief `brief-20251215-eyum` wasn't found in SKEIN. Let me search more broadly:

---

# Design Notes

## Format Goals

1. **Readable** - Clean markdown that renders well in GitHub, VSCode, etc.
2. **Searchable** - All text content preserved for grep/search
3. **Minimal noise** - Tool calls, tool results, and thinking blocks are omitted
4. **Preserves conversation flow** - User → Claude → User pattern is clear

## What's Included

- **Header**: Session ID, project name, directory, date
- **User messages**: The actual text content from the human
- **Claude messages**: Only the text responses, not tool calls

## What's Excluded

- `tool_use` blocks (the JSON describing what tool Claude called)
- `tool_result` blocks (output from tools)
- `thinking` blocks (Claude's internal reasoning)
- `queue-operation` entries (internal CC housekeeping)
- Empty messages (e.g., a message that was purely a tool result)

## Alternative Formats

### Compact (for archiving)

```markdown
**User**: What is 2+2?
**Claude**: 4
```

### With timestamps

```markdown
## 👤 User (2025-12-16 04:07:05)
What is 2+2?

## 🤖 Claude (2025-12-16 04:07:06)
4
```

### With tool summaries (show what happened, not the JSON)

```markdown
## 🤖 Claude
I'll check the file structure.

> *Used: Read file `src/main.py`*
> *Used: Bash `git status`*

The main module handles...
```

## Proposed CLI

```bash
# Export by session number (from shuttle ls)
shuttle export 1

# Export by session UUID
shuttle export f900e8fd-2921-4714-b10d-265d5263f6a9

# Write to file instead of stdout
shuttle export 1 --file session.md

# JSON format for programmatic use
shuttle export 1 --json

# Only last N messages
shuttle export 1 --last 20

# Include tool summaries
shuttle export 1 --with-tools
```
