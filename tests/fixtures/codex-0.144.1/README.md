# Codex 0.144.1 hook fixtures

These payloads use the common and event-specific fields emitted by the locally
installed `codex-cli 0.144.1` hook interface and documented in the Codex Hooks
release reference on 2026-07-12. Values are synthetic; field names and JSON
types are provider-shaped.

- `SessionStart`: common fields plus `source`
- `UserPromptSubmit`: common fields plus `turn_id` and `prompt`
- `PermissionRequest`: common fields plus `turn_id`, `tool_name`, and
  `tool_input`
- `Stop`: common fields plus `turn_id`, `stop_hook_active`, and
  `last_assistant_message`
