"""Tests for extract_pairs.py.

Uses synthetic JSONL data; no real session files required.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from .extract_pairs import _INJECTION_PREFIXES, extract_pairs
from .conftest import (
    make_assistant_message,
    make_event,
    make_text,
    make_tool_result,
    make_tool_use,
    make_user_message,
    write_jsonl,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _session(tmp_path, events, name="session.jsonl"):
    return write_jsonl(tmp_path / name, events)


# ---------------------------------------------------------------------------
# isMeta events must not produce pairs
# ---------------------------------------------------------------------------

class TestIsMetaFiltering:
    """isMeta user events should be invisible to extract_pairs."""

    def test_is_meta_user_event_no_pair(self, tmp_path):
        """isMeta user event with type 'user' must not emit a pair."""
        events = [
            # isMeta event: type=user, but not human input
            make_event("m1", None, "user",
                       message={"role": "user", "content": "some system context"},
                       isMeta=True),
            make_event("a1", "m1", "assistant",
                       message=make_assistant_message(
                           [make_text("Here is my response.")],
                           msg_id="msg_001", stop_reason="end_turn")),
        ]
        path = _session(tmp_path, events)
        pairs = list(extract_pairs(path, min_context=0))
        assert pairs == [], "isMeta event must not produce a pair"

    def test_is_meta_does_not_pollute_context(self, tmp_path):
        """isMeta events must not appear in the context of subsequent pairs."""
        events = [
            make_event("m1", None, "user",
                       message={"role": "user", "content": "injected context"},
                       isMeta=True),
            make_event("u1", "m1", "user",
                       message=make_user_message([make_text("Hello")])),
            make_event("a1", "u1", "assistant",
                       message=make_assistant_message(
                           [make_text("Hi!")],
                           msg_id="msg_001", stop_reason="end_turn")),
            make_event("u2", "a1", "user",
                       message=make_user_message([make_text("How are you?")])),
        ]
        path = _session(tmp_path, events)
        # min_context=1: only the second human message gets a pair
        pairs = list(extract_pairs(path, min_context=1))
        assert len(pairs) == 1
        # Context must contain only real turns, not the meta event
        roles_in_context = [m["role"] for m in pairs[0].context]
        contents_in_context = [m["content"] for m in pairs[0].context]
        assert "injected context" not in contents_in_context
        assert pairs[0].response == "How are you?"


# ---------------------------------------------------------------------------
# Injection-only user messages must not produce pairs
# ---------------------------------------------------------------------------

class TestInjectionFiltering:
    """User messages that are purely CC-injected text must be skipped."""

    @pytest.mark.parametrize("prefix", list(_INJECTION_PREFIXES))
    def test_injection_prefix_skipped(self, tmp_path, prefix):
        """Each injection prefix must suppress the pair when it's the only text."""
        injection_text = f"{prefix}some injected content here"
        events = [
            make_event("u1", None, "user",
                       message=make_user_message([make_text("Setup")])),
            make_event("a1", "u1", "assistant",
                       message=make_assistant_message(
                           [make_text("OK")],
                           msg_id="msg_001", stop_reason="end_turn")),
            make_event("u2", "a1", "user",
                       message=make_user_message([make_text(injection_text)])),
        ]
        path = _session(tmp_path, events)
        pairs = list(extract_pairs(path, min_context=1))
        # u2 is injection-only, should not produce a pair
        assert all(p.response != injection_text for p in pairs), (
            f"Injection message starting with '{prefix}' must not become a pair"
        )


# ---------------------------------------------------------------------------
# Valid human messages do produce pairs
# ---------------------------------------------------------------------------

class TestValidPairs:
    """Normal human messages after assistant context must produce pairs."""

    def test_valid_pair_emitted(self, tmp_path):
        """A real human message after one assistant turn produces a pair."""
        events = [
            make_event("u1", None, "user",
                       message=make_user_message([make_text("What is 2+2?")])),
            make_event("a1", "u1", "assistant",
                       message=make_assistant_message(
                           [make_text("It's 4.")],
                           msg_id="msg_001", stop_reason="end_turn")),
            make_event("u2", "a1", "user",
                       message=make_user_message([make_text("Why?")])),
        ]
        path = _session(tmp_path, events)
        pairs = list(extract_pairs(path, min_context=1))
        assert len(pairs) == 1
        assert pairs[0].response == "Why?"
        assert len(pairs[0].context) == 2
        assert pairs[0].context[0] == {"role": "user", "content": "What is 2+2?"}
        assert pairs[0].context[1] == {"role": "assistant", "content": "It's 4."}

    def test_pair_session_metadata(self, tmp_path):
        """Pair carries correct session_id, project, and timestamp."""
        events = [
            make_event("u1", None, "user",
                       message=make_user_message([make_text("Start")])),
            make_event("a1", "u1", "assistant",
                       message=make_assistant_message(
                           [make_text("Going.")],
                           msg_id="msg_001", stop_reason="end_turn")),
            make_event("u2", "a1", "user",
                       message=make_user_message([make_text("Continue")])),
        ]
        path = _session(tmp_path, events)
        pairs = list(extract_pairs(path, min_context=1))
        assert len(pairs) == 1
        p = pairs[0]
        assert p.session_id == "test-session-001"
        assert p.project == "test"           # basename of cwd="/tmp/test"
        assert p.timestamp                   # non-empty ISO string


# ---------------------------------------------------------------------------
# Edge cases: empty and tool-only sessions
# ---------------------------------------------------------------------------

class TestEdgeCases:
    """Degenerate sessions should yield no pairs."""

    def test_empty_session_no_pairs(self, tmp_path):
        """Empty session file produces no pairs."""
        path = tmp_path / "empty.jsonl"
        path.write_text("")
        pairs = list(extract_pairs(path, min_context=0))
        assert pairs == []

    def test_tool_results_only_no_pairs(self, tmp_path):
        """Session where all user messages are tool results produces no pairs."""
        events = [
            make_event("u1", None, "user",
                       message=make_user_message([make_text("Run a command")])),
            make_event("a1", "u1", "assistant",
                       message=make_assistant_message(
                           [make_tool_use("t1", "Bash", {"command": "ls"})],
                           msg_id="msg_001", stop_reason="tool_use")),
            # User event with only tool_result content — no human text
            make_event("u2", "a1", "user",
                       message=make_user_message(
                           [make_tool_result("t1", "file1.py\nfile2.py")])),
            make_event("a2", "u2", "assistant",
                       message=make_assistant_message(
                           [make_text("Found the files.")],
                           msg_id="msg_002", stop_reason="end_turn")),
        ]
        path = _session(tmp_path, events)
        # min_context=0 so u1 is eligible, but u2 has no human text
        pairs = list(extract_pairs(path, min_context=0))
        # Only u1 produces a pair (it's the first human message with min_context=0)
        assert len(pairs) == 1
        assert pairs[0].response == "Run a command"
        # Confirm u2 (tool result only) did not produce a pair
        assert all(p.response != "" for p in pairs)

    def test_session_with_only_tool_exchanges_no_second_pair(self, tmp_path):
        """After initial human message, if all subsequent user msgs are tool results, only one pair."""
        events = [
            make_event("u1", None, "user",
                       message=make_user_message([make_text("Do work")])),
            make_event("a1", "u1", "assistant",
                       message=make_assistant_message(
                           [make_tool_use("t1", "Read", {"file_path": "/tmp/f.py"})],
                           msg_id="msg_001", stop_reason="tool_use")),
            make_event("u2", "a1", "user",
                       message=make_user_message(
                           [make_tool_result("t1", "content")])),
            make_event("a2", "u2", "assistant",
                       message=make_assistant_message(
                           [make_tool_use("t2", "Read", {"file_path": "/tmp/g.py"})],
                           msg_id="msg_002", stop_reason="tool_use")),
            make_event("u3", "a2", "user",
                       message=make_user_message(
                           [make_tool_result("t2", "more content")])),
            make_event("a3", "u3", "assistant",
                       message=make_assistant_message(
                           [make_text("Done.")],
                           msg_id="msg_003", stop_reason="end_turn")),
        ]
        path = _session(tmp_path, events)
        pairs = list(extract_pairs(path, min_context=1))
        # u1 has no prior context (min_context=1 skips it); u2, u3 are tool-only
        assert pairs == []
