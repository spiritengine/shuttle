"""Hardening tests - these define desired behavior that the current implementation doesn't fully handle.

Written to FAIL against current code, defining the fix targets.
"""

import json
import math

import pytest

from . import (
    Event,
    Session,
    estimate_tokens,
    load,
    parse,
    serialize,
    serialize_to_bytes,
)
from .conftest import (
    make_assistant_message,
    make_event,
    make_text,
    make_thinking,
    make_tool_result,
    make_tool_use,
    make_user_message,
    write_jsonl,
)


# ============================================================
# Relink Chain: Topological Correctness
# ============================================================


class TestRelinkTopological:
    """relink_chain should walk up the ORIGINAL chain to find the nearest
    surviving ancestor, not just point to the previous event in file order.

    Current behavior: points to events[idx-1], which is wrong for sparse removal.
    Desired: walk original parentUuid chain upward until finding a UUID still in the set.
    """

    def test_sparse_removal_finds_correct_ancestor(self, tmp_jsonl, tmp_path):
        """Removing a non-contiguous middle event should relink to its parent's parent,
        not to an arbitrary previous event."""
        # Chain: u1 -> a1 -> u2 -> a2 -> u3 -> a3
        events = [
            make_event("u1", None, "user",
                       message=make_user_message([make_text("first")])),
            make_event("a1", "u1", "assistant",
                       message=make_assistant_message([make_text("resp1")],
                                                     msg_id="m1", stop_reason="end_turn")),
            make_event("u2", "a1", "user",
                       message=make_user_message([make_text("second")])),
            make_event("a2", "u2", "assistant",
                       message=make_assistant_message([make_text("resp2")],
                                                     msg_id="m2", stop_reason="end_turn")),
            make_event("u3", "a2", "user",
                       message=make_user_message([make_text("third")])),
            make_event("a3", "u3", "assistant",
                       message=make_assistant_message([make_text("resp3")],
                                                     msg_id="m3", stop_reason="end_turn")),
        ]
        path = tmp_jsonl(events)
        session = load(path)

        # Remove u2 and a2 (middle of chain)
        session.remove_events(lambda e: e.uuid in ("u2", "a2"))

        out = tmp_path / "sparse.jsonl"
        serialize(session, out)
        session2 = load(out)

        # u3 should now point to a1 (nearest surviving ancestor in original chain)
        # NOT to the previous event in file order (which would also be a1 in this case,
        # but for the right reason)
        u3 = [e for e in session2.events if e.uuid == "u3"][0]
        assert u3.parent_uuid == "a1", \
            f"u3 should point to a1 (ancestor), got {u3.parent_uuid}"

        # Chain must be walkable
        chain = session2.tree.walk_chain_to_root(session2.events[-1])
        assert chain[0].parent_uuid is None

    def test_non_contiguous_removal_correct_ancestors(self, tmp_jsonl, tmp_path):
        """Remove events scattered through chain - each orphan finds its own correct ancestor."""
        # Chain: u1 -> a1 -> s1 -> u2 -> a2 -> s2 -> u3 -> a3
        # Remove s1 and s2 (system events scattered in chain)
        events = [
            make_event("u1", None, "user",
                       message=make_user_message([make_text("q1")])),
            make_event("a1", "u1", "assistant",
                       message=make_assistant_message([make_text("r1")],
                                                     msg_id="m1", stop_reason="end_turn")),
            make_event("s1", "a1", "system", subtype="stop_hook_summary",
                       hookCount=0, hookInfos=[], hookErrors=[],
                       preventedContinuation=False, stopReason="", hasOutput=False),
            make_event("u2", "s1", "user",
                       message=make_user_message([make_text("q2")])),
            make_event("a2", "u2", "assistant",
                       message=make_assistant_message([make_text("r2")],
                                                     msg_id="m2", stop_reason="end_turn")),
            make_event("s2", "a2", "system", subtype="turn_duration", durationMs=5000),
            make_event("u3", "s2", "user",
                       message=make_user_message([make_text("q3")])),
            make_event("a3", "u3", "assistant",
                       message=make_assistant_message([make_text("r3")],
                                                     msg_id="m3", stop_reason="end_turn")),
        ]
        path = tmp_jsonl(events)
        session = load(path)
        session.remove_events(lambda e: e.type == "system")

        out = tmp_path / "scattered.jsonl"
        serialize(session, out)
        session2 = load(out)

        # u2 should point to a1 (s1's parent)
        u2 = [e for e in session2.events if e.uuid == "u2"][0]
        assert u2.parent_uuid == "a1"

        # u3 should point to a2 (s2's parent)
        u3 = [e for e in session2.events if e.uuid == "u3"][0]
        assert u3.parent_uuid == "a2"

    def test_multi_hop_removal(self, tmp_jsonl, tmp_path):
        """Removing multiple consecutive events requires walking up multiple hops."""
        # Chain: u1 -> p1 -> p2 -> p3 -> u2
        # Remove p1, p2, p3 - u2 should point to u1
        events = [
            make_event("u1", None, "user",
                       message=make_user_message([make_text("start")])),
            make_event("p1", "u1", "progress",
                       data={"type": "bash_progress", "output": "1"}),
            make_event("p2", "p1", "progress",
                       data={"type": "bash_progress", "output": "2"}),
            make_event("p3", "p2", "progress",
                       data={"type": "bash_progress", "output": "3"}),
            make_event("u2", "p3", "user",
                       message=make_user_message([make_tool_result("t1", "done")])),
        ]
        path = tmp_jsonl(events)
        session = load(path)
        session.remove_events(lambda e: e.type == "progress")

        out = tmp_path / "multihop.jsonl"
        serialize(session, out)
        session2 = load(out)

        u2 = [e for e in session2.events if e.uuid == "u2"][0]
        assert u2.parent_uuid == "u1", \
            f"u2 should hop 3 levels up to u1, got {u2.parent_uuid}"


# ============================================================
# Validation / Robustness
# ============================================================


class TestValidation:
    """Parser should handle malformed input gracefully."""

    def test_malformed_json_line_raises(self, tmp_path):
        """Non-JSON line should raise a clear error."""
        path = tmp_path / "bad.jsonl"
        path.write_text('{"type": "user", "uuid": "u1"}\nNOT JSON\n{"type": "user"}\n')
        with pytest.raises((json.JSONDecodeError, ValueError)):
            list(parse(path))

    def test_missing_uuid_gets_empty_string(self, tmp_jsonl):
        """Event without uuid field gets empty string (not crash)."""
        events = [{"type": "user", "parentUuid": None, "timestamp": "2026-01-01T00:00:00Z",
                   "sessionId": "s", "isSidechain": False, "cwd": "/", "version": "1",
                   "gitBranch": "main",
                   "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}}]
        path = tmp_jsonl(events)
        result = list(parse(path))
        assert len(result) == 1
        assert result[0].uuid == ""

    def test_circular_parent_uuid_no_infinite_loop(self, tmp_jsonl):
        """Circular parentUuid doesn't cause infinite loop in walk_chain_to_root."""
        events = [
            make_event("a", "b", "user",
                       message=make_user_message([make_text("hi")])),
            make_event("b", "a", "assistant",
                       message=make_assistant_message([make_text("hello")],
                                                     msg_id="m1")),
        ]
        path = tmp_jsonl(events)
        session = load(path)
        # Should terminate (cycle detection)
        chain = session.tree.walk_chain_to_root(session.events[0])
        assert len(chain) <= 2  # At most visits each once

    def test_null_message_content(self, tmp_jsonl):
        """Event with null content in message doesn't crash."""
        events = [{"type": "user", "uuid": "u1", "parentUuid": None,
                   "timestamp": "2026-01-01T00:00:00Z", "sessionId": "s",
                   "isSidechain": False, "cwd": "/", "version": "1", "gitBranch": "main",
                   "message": {"role": "user", "content": None}}]
        path = tmp_jsonl(events)
        result = list(parse(path))
        assert result[0].message.content == []

    def test_content_not_list_or_string(self, tmp_jsonl):
        """Event with unexpected content type (number) doesn't crash."""
        events = [{"type": "user", "uuid": "u1", "parentUuid": None,
                   "timestamp": "2026-01-01T00:00:00Z", "sessionId": "s",
                   "isSidechain": False, "cwd": "/", "version": "1", "gitBranch": "main",
                   "message": {"role": "user", "content": 42}}]
        path = tmp_jsonl(events)
        result = list(parse(path))
        assert result[0].message.content == []

    def test_missing_message_field(self, tmp_jsonl):
        """Event without message field gets message=None."""
        events = [{"type": "progress", "uuid": "p1", "parentUuid": None,
                   "timestamp": "2026-01-01T00:00:00Z", "sessionId": "s",
                   "isSidechain": False, "cwd": "/", "version": "1", "gitBranch": "main",
                   "data": {"type": "bash_progress"}}]
        path = tmp_jsonl(events)
        result = list(parse(path))
        assert result[0].message is None

    def test_tool_result_content_is_list(self, tmp_jsonl):
        """Some tool results have content as list of objects (not string)."""
        events = [
            make_event("u1", None, "user",
                       message=make_user_message([make_text("hi")])),
            make_event("a1", "u1", "assistant",
                       message=make_assistant_message(
                           [make_tool_use("t1", "Read", {"file_path": "/x"})],
                           msg_id="m1", stop_reason="tool_use")),
            make_event("u2", "a1", "user",
                       message={"role": "user", "content": [
                           {"type": "tool_result", "tool_use_id": "t1",
                            "content": [{"type": "text", "text": "file content here"}],
                            "is_error": False}
                       ]}),
        ]
        path = tmp_jsonl(events)
        session = load(path)
        # Should not crash, content_size should handle gracefully
        pair = session.tool_pairs()[0]
        # content is a list, not string - content_size should be 0 or handle it
        assert pair.tool_result_block.content_size >= 0


# ============================================================
# isMeta Filtering
# ============================================================


class TestMetaFiltering:
    """isMeta events should be distinguishable from real user messages."""

    def test_session_has_user_messages_method(self, tmp_jsonl):
        """Session should expose a way to get only non-meta user messages."""
        events = [
            make_event("m1", None, "user",
                       message={"role": "user",
                                "content": "<local-command-caveat>System noise</local-command-caveat>"},
                       isMeta=True),
            make_event("u1", "m1", "user",
                       message=make_user_message([make_text("Real question")])),
            make_event("a1", "u1", "assistant",
                       message=make_assistant_message([make_text("Answer")],
                                                     msg_id="m1", stop_reason="end_turn")),
        ]
        path = tmp_jsonl(events)
        session = load(path)

        # Should have a way to get user intent messages only
        user_messages = [e for e in session.events
                         if e.type == "user" and not e.is_meta and e.is_conversation]
        assert len(user_messages) == 1
        assert user_messages[0].message.content[0].text == "Real question"


# ============================================================
# Tool Result Content as List
# ============================================================


class TestToolResultContentList:
    """Tool result content can be a list of content blocks, not just a string.
    The parser should handle this gracefully.
    """

    def test_list_content_accessible(self, tmp_jsonl):
        """When tool_result content is a list, should be accessible."""
        events = [
            make_event("u1", None, "user",
                       message=make_user_message([make_text("hi")])),
            make_event("a1", "u1", "assistant",
                       message=make_assistant_message(
                           [make_tool_use("t1", "Read", {"file_path": "/x"})],
                           msg_id="m1", stop_reason="tool_use")),
            make_event("u2", "a1", "user",
                       message={"role": "user", "content": [
                           {"type": "tool_result", "tool_use_id": "t1",
                            "content": [{"type": "text", "text": "line1\nline2"}],
                            "is_error": False}
                       ]}),
        ]
        path = tmp_jsonl(events)
        session = load(path)
        block = session.tool_pairs()[0].tool_result_block

        # content property returns the raw value
        # For list content, should return the list or extract text
        raw_content = block.raw.get("content")
        assert isinstance(raw_content, list)
        # The content property currently returns None for non-string content
        # It SHOULD handle list content gracefully
        assert block.content_size >= 0  # At minimum, don't crash


# ============================================================
# Remove Root Event
# ============================================================


class TestRemoveRoot:
    """Removing the root event should promote the next event to root."""

    def test_remove_root_promotes_child(self, tmp_jsonl, tmp_path):
        """If root event is removed, its child becomes new root."""
        events = [
            make_event("u1", None, "user",
                       message=make_user_message([make_text("first")])),
            make_event("a1", "u1", "assistant",
                       message=make_assistant_message([make_text("resp")],
                                                     msg_id="m1", stop_reason="end_turn")),
            make_event("u2", "a1", "user",
                       message=make_user_message([make_text("second")])),
        ]
        path = tmp_jsonl(events)
        session = load(path)
        session.remove_events(lambda e: e.uuid == "u1")

        out = tmp_path / "no_root.jsonl"
        serialize(session, out)
        session2 = load(out)

        # a1 should now be root (parentUuid = None)
        assert session2.events[0].uuid == "a1"
        assert session2.events[0].parent_uuid is None

        # Chain should still work
        chain = session2.tree.walk_chain_to_root(session2.events[-1])
        assert chain[0].parent_uuid is None


# ============================================================
# Duplicate Tool Use IDs
# ============================================================


class TestDuplicateIds:
    """Edge case: what if two tool_results have the same tool_use_id?"""

    def test_duplicate_tool_result_ids(self, tmp_jsonl):
        """Two tool_results with same tool_use_id - first one wins."""
        events = [
            make_event("u1", None, "user",
                       message=make_user_message([make_text("hi")])),
            make_event("a1", "u1", "assistant",
                       message=make_assistant_message(
                           [make_tool_use("t1", "Bash", {"command": "echo hi"})],
                           msg_id="m1", stop_reason="tool_use")),
            make_event("u2", "a1", "user",
                       message=make_user_message(
                           [make_tool_result("t1", "first result")])),
            # Somehow a second result with same ID (shouldn't happen, but be robust)
            make_event("u3", "u2", "user",
                       message=make_user_message(
                           [make_tool_result("t1", "second result")])),
        ]
        path = tmp_jsonl(events)
        session = load(path)
        pairs = session.tool_pairs()
        # Should not crash, should produce at least one pair
        assert len(pairs) >= 1
