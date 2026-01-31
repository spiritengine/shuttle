# Feature: shuttle whoami / confirm

## Problem

Agents can't easily discover their own session ID. When an agent runs commands, the output goes to their session file, but they don't know which file that is.

## Solution

Two-step probe mechanism:

### `shuttle whoami`

1. Generate a unique probe string (e.g., `SESSION_PROBE_<random>`)
2. Print it along with instructions:
   ```
   SESSION_PROBE_x7f3a2b1
   Run: shuttle confirm x7f3a2b1
   ```

### `shuttle confirm <probe>`

1. Search recent session files for the probe string
2. The file containing it is the caller's session
3. Return the session ID:
   ```
   You are: 593d18c8-d1db-4d3e-9a42-90a56ee8f589
   ```

## Why it works

- The `whoami` output gets written to the calling session's JSONL file as tool result
- Only ONE session will contain that exact probe string
- `confirm` greps for it and returns the match

## Implementation notes

- Probe should be short but unique enough (8-12 random chars)
- Search should look in `~/.claude/projects/*/` 
- Only search recent files (last hour?) to keep it fast
- Handle edge case: probe not found (session too new, not flushed yet)

## Testing

```bash
# From a Claude session:
shuttle whoami
# See the probe
shuttle confirm <probe>
# Should return current session ID
```
