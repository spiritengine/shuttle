"""Tests for CC session JSONL parser.

Covers: parsing, tree construction, tool matching, round-trip serialization,
chain relinking, microcompaction awareness, token estimation.
"""

import json
import math
from pathlib import Path

import pytest

from . import (
    CLEARED_MARKER,
    ContentBlock,
    Event,
    EventTree,
    Session,
    ToolPair,
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
# Basic Parsing
# ============================================================


class TestParsing:
    """Basic JSONL parsing into typed events."""

    def test_parse_minimal(self, minimal_events, tmp_jsonl):
        """Parse minimal 2-event session."""
        path = tmp_jsonl(minimal_events)
        events = list(parse(path))
        assert len(events) == 2

    def test_event_types(self, minimal_events, tmp_jsonl):
        """Events have correct types."""
        path = tmp_jsonl(minimal_events)
        events = list(parse(path))
        assert events[0].type == "user"
        assert events[1].type == "assistant"

    def test_event_uuid(self, minimal_events, tmp_jsonl):
        """Events expose uuid and parent_uuid."""
        path = tmp_jsonl(minimal_events)
        events = list(parse(path))
        assert events[0].uuid == "u1"
        assert events[0].parent_uuid is None
        assert events[1].uuid == "a1"
        assert events[1].parent_uuid == "u1"

    def test_event_metadata(self, minimal_events, tmp_jsonl):
        """Events expose session metadata."""
        path = tmp_jsonl(minimal_events)
        events = list(parse(path))
        assert events[0].session_id == "test-session-001"
        assert events[0].cwd == "/tmp/test"
        assert events[0].git_branch == "main"
        assert events[0].version == "2.1.14"
        assert events[0].is_sidechain is False

    def test_user_message_content(self, minimal_events, tmp_jsonl):
        """User message has text content block."""
        path = tmp_jsonl(minimal_events)
        events = list(parse(path))
        msg = events[0].message
        assert msg is not None
        assert msg.role == "user"
        assert len(msg.content) == 1
        assert msg.content[0].is_text
        assert msg.content[0].text == "Hello"

    def test_assistant_message_content(self, minimal_events, tmp_jsonl):
        """Assistant message has thinking + text blocks."""
        path = tmp_jsonl(minimal_events)
        events = list(parse(path))
        msg = events[1].message
        assert msg is not None
        assert msg.role == "assistant"
        assert len(msg.content) == 2
        assert msg.content[0].is_thinking
        assert msg.content[0].thinking == "Let me help"
        assert msg.content[1].is_text
        assert msg.content[1].text == "Hi there!"

    def test_assistant_message_metadata(self, minimal_events, tmp_jsonl):
        """Assistant message has API metadata."""
        path = tmp_jsonl(minimal_events)
        events = list(parse(path))
        msg = events[1].message
        assert msg.id == "msg_001"
        assert msg.stop_reason == "end_turn"

    def test_tool_use_block(self, tool_session_events, tmp_jsonl):
        """Tool use block has id, name, input."""
        path = tmp_jsonl(tool_session_events)
        events = list(parse(path))
        msg = events[1].message  # assistant with tool_use
        tool_block = msg.content[1]  # [0] is thinking
        assert tool_block.is_tool_use
        assert tool_block.tool_use_id == "t1"
        assert tool_block.name == "Read"
        assert tool_block.input == {"file_path": "/tmp/foo.py"}

    def test_tool_result_block(self, tool_session_events, tmp_jsonl):
        """Tool result block has tool_use_id, content, is_error."""
        path = tmp_jsonl(tool_session_events)
        events = list(parse(path))
        msg = events[2].message  # user with tool_result
        result_block = msg.content[0]
        assert result_block.is_tool_result
        assert result_block.tool_use_id == "t1"
        assert result_block.content == "def foo():\n    return 42\n"
        assert result_block.is_error is False

    def test_progress_event_no_message(self, progress_session_events, tmp_jsonl):
        """Progress events have no parsed message."""
        path = tmp_jsonl(progress_session_events)
        events = list(parse(path))
        progress = [e for e in events if e.type == "progress"]
        assert len(progress) == 2
        assert progress[0].message is None

    def test_meta_string_content(self, meta_session_events, tmp_jsonl):
        """isMeta events with string content are parsed as text blocks."""
        path = tmp_jsonl(meta_session_events)
        events = list(parse(path))
        meta = events[0]
        assert meta.is_meta is True
        assert meta.message.content[0].is_text
        assert "Caveat" in meta.message.content[0].text

    def test_is_conversation(self, progress_session_events, tmp_jsonl):
        """is_conversation filters non-API events."""
        path = tmp_jsonl(progress_session_events)
        events = list(parse(path))
        conv = [e for e in events if e.is_conversation]
        non_conv = [e for e in events if not e.is_conversation]
        assert all(e.type in ("user", "assistant") for e in conv)
        assert all(e.type == "progress" for e in non_conv)

    def test_raw_preserved(self, tool_session_events, tmp_jsonl):
        """Events preserve complete raw dict."""
        path = tmp_jsonl(tool_session_events)
        events = list(parse(path))
        # Raw should have all original keys
        assert events[0].raw["uuid"] == "u1"
        assert events[0].raw["type"] == "user"
        assert events[0].raw["sessionId"] == "test-session-001"

    def test_parse_iterator(self, tool_session_events, tmp_jsonl):
        """parse() returns an iterator, not a list."""
        path = tmp_jsonl(tool_session_events)
        result = parse(path)
        # Should be an iterator
        assert hasattr(result, '__next__')
        first = next(result)
        assert isinstance(first, Event)

    def test_empty_file(self, tmp_path):
        """Empty file produces no events."""
        path = tmp_path / "empty.jsonl"
        path.write_text("")
        events = list(parse(path))
        assert events == []

    def test_unknown_fields_preserved(self, tmp_jsonl):
        """Unknown/future fields in raw JSON are preserved."""
        events = [
            make_event("u1", None, "user",
                       message=make_user_message([make_text("hi")]),
                       futureField="must_survive",
                       nestedFuture={"a": 1, "b": [2, 3]}),
        ]
        path = tmp_jsonl(events)
        parsed = list(parse(path))
        assert parsed[0].raw["futureField"] == "must_survive"
        assert parsed[0].raw["nestedFuture"] == {"a": 1, "b": [2, 3]}


# ============================================================
# Tree Construction
# ============================================================


class TestTree:
    """Tree construction via uuid/parentUuid."""

    def test_roots_identified(self, minimal_events, tmp_jsonl):
        """Root events (parentUuid=None) are identified."""
        path = tmp_jsonl(minimal_events)
        session = load(path)
        assert len(session.tree.roots) == 1
        assert session.tree.roots[0].uuid == "u1"

    def test_children_computed(self, minimal_events, tmp_jsonl):
        """Children are populated on each event."""
        path = tmp_jsonl(minimal_events)
        session = load(path)
        root = session.events[0]
        assert len(root.children) == 1
        assert root.children[0].uuid == "a1"

    def test_get_by_uuid(self, tool_session_events, tmp_jsonl):
        """Tree.get() looks up events by UUID."""
        path = tmp_jsonl(tool_session_events)
        session = load(path)
        event = session.tree.get("a1")
        assert event is not None
        assert event.type == "assistant"

    def test_walk_chain_to_root(self, tool_session_events, tmp_jsonl):
        """walk_chain_to_root traces parentUuid from leaf."""
        path = tmp_jsonl(tool_session_events)
        session = load(path)
        leaf = session.events[-1]  # a2
        chain = session.tree.walk_chain_to_root(leaf)
        assert chain[0].parent_uuid is None  # root
        assert chain[-1].uuid == leaf.uuid  # leaf
        assert len(chain) == 4  # u1 -> a1 -> u2 -> a2

    def test_walk_chain_with_progress(self, progress_session_events, tmp_jsonl):
        """Chain walks through progress events."""
        path = tmp_jsonl(progress_session_events)
        session = load(path)
        leaf = session.events[-1]
        chain = session.tree.walk_chain_to_root(leaf)
        assert chain[0].parent_uuid is None
        assert chain[-1].uuid == leaf.uuid
        # Chain includes progress events
        types = [e.type for e in chain]
        assert "progress" in types

    def test_branching_multi_tool(self, multi_tool_events, tmp_jsonl):
        """Multiple tool results can branch from one parent."""
        path = tmp_jsonl(multi_tool_events)
        session = load(path)
        # a1 has one child: u2 (which contains both tool results)
        a1 = session.tree.get("a1")
        assert len(a1.children) == 1

    def test_streaming_chain(self, streaming_session_events, tmp_jsonl):
        """Streaming events form parent-child chain."""
        path = tmp_jsonl(streaming_session_events)
        session = load(path)
        a1 = session.tree.get("a1")
        assert len(a1.children) == 1
        assert a1.children[0].uuid == "a2"  # a2 is child of a1

    def test_rebuild_after_removal(self, progress_session_events, tmp_jsonl):
        """Tree rebuild works after removing events."""
        path = tmp_jsonl(progress_session_events)
        session = load(path)
        original_count = len(session.events)
        session.remove_events(lambda e: e.type == "progress")
        assert len(session.events) < original_count
        # Tree is rebuilt
        assert len(session.tree.roots) >= 1


# ============================================================
# Tool Matching
# ============================================================


class TestToolMatching:
    """Tool use/result pairing."""

    def test_single_pair(self, tool_session_events, tmp_jsonl):
        """Single tool_use matched to tool_result."""
        path = tmp_jsonl(tool_session_events)
        session = load(path)
        pairs = session.tool_pairs()
        assert len(pairs) == 1
        assert pairs[0].tool_name == "Read"
        assert pairs[0].tool_use_id == "t1"

    def test_multi_pair(self, multi_tool_events, tmp_jsonl):
        """Multiple tool_use/result pairs matched."""
        path = tmp_jsonl(multi_tool_events)
        session = load(path)
        pairs = session.tool_pairs()
        assert len(pairs) == 2
        names = {p.tool_name for p in pairs}
        assert names == {"Read"}

    def test_pair_metadata_file_path(self, tool_session_events, tmp_jsonl):
        """ToolPair exposes file_path for Read tool."""
        path = tmp_jsonl(tool_session_events)
        session = load(path)
        pair = session.tool_pairs()[0]
        assert pair.file_path == "/tmp/foo.py"

    def test_pair_metadata_command(self, tmp_jsonl):
        """ToolPair exposes command for Bash tool."""
        events = [
            make_event("u1", None, "user",
                       message=make_user_message([make_text("run it")])),
            make_event("a1", "u1", "assistant",
                       message=make_assistant_message(
                           [make_tool_use("t1", "Bash", {"command": "ls -la"})],
                           msg_id="msg_001", stop_reason="tool_use")),
            make_event("u2", "a1", "user",
                       message=make_user_message(
                           [make_tool_result("t1", "total 42\n-rw-r--r-- ...")])),
        ]
        path = tmp_jsonl(events)
        session = load(path)
        pair = session.tool_pairs()[0]
        assert pair.tool_name == "Bash"
        assert pair.command == "ls -la"

    def test_pair_metadata_pattern(self, tmp_jsonl):
        """ToolPair exposes pattern for Grep tool."""
        events = [
            make_event("u1", None, "user",
                       message=make_user_message([make_text("search")])),
            make_event("a1", "u1", "assistant",
                       message=make_assistant_message(
                           [make_tool_use("t1", "Grep", {"pattern": "def foo", "path": "/tmp"})],
                           msg_id="msg_001", stop_reason="tool_use")),
            make_event("u2", "a1", "user",
                       message=make_user_message(
                           [make_tool_result("t1", "/tmp/a.py:5:def foo():")])),
        ]
        path = tmp_jsonl(events)
        session = load(path)
        pair = session.tool_pairs()[0]
        assert pair.tool_name == "Grep"
        assert pair.pattern == "def foo"

    def test_pair_result_size(self, tool_session_events, tmp_jsonl):
        """ToolPair reports result size in chars."""
        path = tmp_jsonl(tool_session_events)
        session = load(path)
        pair = session.tool_pairs()[0]
        assert pair.result_size == len("def foo():\n    return 42\n")

    def test_unmatched_tool_use_ignored(self, tmp_jsonl):
        """tool_use without matching tool_result doesn't produce a pair."""
        events = [
            make_event("u1", None, "user",
                       message=make_user_message([make_text("hi")])),
            make_event("a1", "u1", "assistant",
                       message=make_assistant_message(
                           [make_tool_use("t1", "Read", {"file_path": "/tmp/x"})],
                           msg_id="msg_001", stop_reason="tool_use")),
            # No tool_result event
        ]
        path = tmp_jsonl(events)
        session = load(path)
        assert len(session.tool_pairs()) == 0

    def test_cleared_pair_detected(self, cleared_session_events, tmp_jsonl):
        """ToolPair.is_cleared detects microcompacted results."""
        path = tmp_jsonl(cleared_session_events)
        session = load(path)
        pair = session.tool_pairs()[0]
        assert pair.is_cleared is True
        assert pair.result_size == 0


# ============================================================
# Session / Messages
# ============================================================


class TestSession:
    """Session-level operations."""

    def test_load(self, tool_session_events, tmp_jsonl):
        """load() returns a Session."""
        path = tmp_jsonl(tool_session_events)
        session = load(path)
        assert isinstance(session, Session)
        assert len(session.events) == 4

    def test_messages_alternation(self, tool_session_events, tmp_jsonl):
        """Reconstructed messages alternate user/assistant."""
        path = tmp_jsonl(tool_session_events)
        session = load(path)
        msgs = session.messages()
        for i in range(1, len(msgs)):
            assert msgs[i]["role"] != msgs[i - 1]["role"]

    def test_messages_merge_streaming(self, streaming_session_events, tmp_jsonl):
        """Streaming events (same role consecutive) are merged."""
        path = tmp_jsonl(streaming_session_events)
        session = load(path)
        msgs = session.messages()
        # u1, a1+a2 merged, u2, a3
        assert len(msgs) == 4
        # First assistant message should have thinking + tool_use merged
        asst_msg = msgs[1]
        assert asst_msg["role"] == "assistant"
        types = [b["type"] for b in asst_msg["content"]]
        assert "thinking" in types
        assert "tool_use" in types

    def test_messages_exclude_progress(self, progress_session_events, tmp_jsonl):
        """Progress events don't appear in messages."""
        path = tmp_jsonl(progress_session_events)
        session = load(path)
        msgs = session.messages()
        # Should be same as tool_session without progress
        assert all(m["role"] in ("user", "assistant") for m in msgs)

    def test_messages_exclude_sidechain(self, tmp_jsonl):
        """isSidechain events excluded from messages."""
        events = [
            make_event("u1", None, "user",
                       message=make_user_message([make_text("hi")])),
            make_event("sc1", "u1", "user",
                       message=make_user_message([make_text("sidechain noise")]),
                       isSidechain=True),
            make_event("a1", "u1", "assistant",
                       message=make_assistant_message(
                           [make_text("hello")],
                           msg_id="msg_001", stop_reason="end_turn")),
        ]
        # Fix the sidechain flag in the raw dict
        events[1]["isSidechain"] = True
        path = tmp_jsonl(events)
        session = load(path)
        msgs = session.messages()
        assert len(msgs) == 2  # u1 and a1 only

    def test_conversation_events(self, progress_session_events, tmp_jsonl):
        """conversation_events() filters to API-relevant events."""
        path = tmp_jsonl(progress_session_events)
        session = load(path)
        conv = session.conversation_events()
        assert all(e.type in ("user", "assistant") for e in conv)
        assert len(conv) == 4

    def test_total_tokens(self, minimal_events, tmp_jsonl):
        """total_tokens() uses CC's formula."""
        path = tmp_jsonl(minimal_events)
        session = load(path)
        tokens = session.total_tokens()
        assert tokens > 0
        # Should be sum of all conversational content
        expected = sum(e.tokens for e in session.events if e.is_conversation)
        assert tokens == expected

    def test_events_by_type(self, progress_session_events, tmp_jsonl):
        """events_by_type() groups correctly."""
        path = tmp_jsonl(progress_session_events)
        session = load(path)
        by_type = session.events_by_type()
        assert "user" in by_type
        assert "assistant" in by_type
        assert "progress" in by_type

    def test_events_by_message_id(self, streaming_session_events, tmp_jsonl):
        """events_by_message_id() groups by API response."""
        path = tmp_jsonl(streaming_session_events)
        session = load(path)
        by_msg = session.events_by_message_id()
        # msg_001 has two events (a1, a2 - streaming)
        assert len(by_msg["msg_001"]) == 2

    def test_remove_events(self, progress_session_events, tmp_jsonl):
        """remove_events() filters and rebuilds tree."""
        path = tmp_jsonl(progress_session_events)
        session = load(path)
        original = len(session.events)
        session.remove_events(lambda e: e.type == "progress")
        assert len(session.events) == original - 2
        # Tool pairs still work
        assert len(session.tool_pairs()) == 1


# ============================================================
# Round-Trip Serialization
# ============================================================


class TestRoundTrip:
    """Parse -> serialize -> parse equivalence."""

    def test_minimal_round_trip(self, minimal_events, tmp_jsonl, tmp_path):
        """Minimal session survives round-trip."""
        path = tmp_jsonl(minimal_events)
        session = load(path)
        out = tmp_path / "out.jsonl"
        serialize(session, out)
        session2 = load(out)
        assert len(session2.events) == len(session.events)
        for e1, e2 in zip(session.events, session2.events):
            assert e1.raw == e2.raw

    def test_tool_session_round_trip(self, tool_session_events, tmp_jsonl, tmp_path):
        """Tool session survives round-trip."""
        path = tmp_jsonl(tool_session_events)
        session = load(path)
        out = tmp_path / "out.jsonl"
        serialize(session, out)
        session2 = load(out)
        assert len(session2.events) == 4
        assert len(session2.tool_pairs()) == 1

    def test_unknown_fields_preserved(self, tmp_jsonl, tmp_path):
        """Unknown fields survive round-trip."""
        events = [
            make_event("u1", None, "user",
                       message=make_user_message([make_text("hi")]),
                       futureField="must_survive",
                       nestedFuture={"a": 1}),
        ]
        path = tmp_jsonl(events)
        session = load(path)
        out = tmp_path / "out.jsonl"
        serialize(session, out)
        session2 = load(out)
        assert session2.events[0].raw["futureField"] == "must_survive"
        assert session2.events[0].raw["nestedFuture"] == {"a": 1}

    def test_enrichment_not_serialized(self, tool_session_events, tmp_jsonl, tmp_path):
        """Computed enrichment fields don't leak into output."""
        path = tmp_jsonl(tool_session_events)
        session = load(path)
        out = tmp_path / "out.jsonl"
        serialize(session, out)
        lines = out.read_text().strip().split("\n")
        for line in lines:
            data = json.loads(line)
            assert "children" not in data
            assert "tool_pair" not in data
            assert "content_size" not in data

    def test_serialize_line_format(self, minimal_events, tmp_jsonl, tmp_path):
        """Output has one JSON object per line, trailing newline."""
        path = tmp_jsonl(minimal_events)
        session = load(path)
        out = tmp_path / "out.jsonl"
        serialize(session, out)
        content = out.read_text()
        assert content.endswith("\n")
        lines = content.strip().split("\n")
        assert len(lines) == 2
        # Each line is valid JSON
        for line in lines:
            json.loads(line)

    def test_serialize_to_bytes(self, tool_session_events, tmp_jsonl):
        """serialize_to_bytes() produces valid JSONL bytes."""
        path = tmp_jsonl(tool_session_events)
        session = load(path)
        data = serialize_to_bytes(session)
        lines = data.decode("utf-8").strip().split("\n")
        assert len(lines) == 4
        for line in lines:
            json.loads(line)

    def test_real_session_round_trip(self, dead_session_path, tmp_path):
        """Full real session survives round-trip."""
        session = load(dead_session_path)
        out = tmp_path / "rt.jsonl"
        serialize(session, out)
        session2 = load(out)
        assert len(session2.events) == len(session.events)
        # Spot check
        for i in [0, 100, 500, 1000, len(session.events) - 1]:
            assert session.events[i].raw == session2.events[i].raw

    def test_post_compact_round_trip(self, post_compact_path, tmp_path):
        """Post-compact session survives round-trip."""
        session = load(post_compact_path)
        out = tmp_path / "rt.jsonl"
        serialize(session, out)
        session2 = load(out)
        assert len(session2.events) == len(session.events)


# ============================================================
# Chain Relinking
# ============================================================


class TestChainRelinking:
    """parentUuid chain integrity after event removal."""

    def test_remove_progress_relinks(self, progress_session_events, tmp_jsonl, tmp_path):
        """Chain remains walkable after removing progress events."""
        path = tmp_jsonl(progress_session_events)
        session = load(path)
        session.remove_events(lambda e: e.type == "progress")

        out = tmp_path / "stripped.jsonl"
        serialize(session, out)
        session2 = load(out)

        # Chain must walk from leaf to root
        chain = session2.tree.walk_chain_to_root(session2.events[-1])
        assert chain[0].parent_uuid is None
        assert chain[-1].uuid == session2.events[-1].uuid

    def test_remove_system_relinks(self, system_session_events, tmp_jsonl, tmp_path):
        """Chain remains walkable after removing system events."""
        path = tmp_jsonl(system_session_events)
        session = load(path)
        session.remove_events(lambda e: e.type == "system")

        out = tmp_path / "stripped.jsonl"
        serialize(session, out)
        session2 = load(out)

        chain = session2.tree.walk_chain_to_root(session2.events[-1])
        assert chain[0].parent_uuid is None

    def test_no_dangling_parent_refs(self, progress_session_events, tmp_jsonl, tmp_path):
        """After serialization, no parentUuid points to nonexistent event."""
        path = tmp_jsonl(progress_session_events)
        session = load(path)
        session.remove_events(lambda e: e.type == "progress")

        out = tmp_path / "stripped.jsonl"
        serialize(session, out)
        session2 = load(out)

        uuid_set = {e.uuid for e in session2.events}
        for event in session2.events:
            if event.parent_uuid is not None:
                assert event.parent_uuid in uuid_set, \
                    f"Event {event.uuid} has dangling parentUuid {event.parent_uuid}"

    def test_logical_parent_set(self, progress_session_events, tmp_jsonl, tmp_path):
        """Events with relinked parents get logicalParentUuid set."""
        path = tmp_jsonl(progress_session_events)
        session = load(path)

        # u2's parent is p2 (a progress event)
        u2 = [e for e in session.events if e.uuid == "u2"][0]
        assert u2.parent_uuid == "p2"  # Points to progress

        session.remove_events(lambda e: e.type == "progress")
        out = tmp_path / "stripped.jsonl"
        serialize(session, out)
        session2 = load(out)

        # The event that was u2 should now have logicalParentUuid
        u2_new = [e for e in session2.events if e.uuid == "u2"][0]
        assert u2_new.raw.get("logicalParentUuid") == "p2"

    def test_tool_pairs_survive_removal(self, progress_session_events, tmp_jsonl, tmp_path):
        """Tool pairs still match after progress removal and reserialize."""
        path = tmp_jsonl(progress_session_events)
        session = load(path)
        original_pairs = len(session.tool_pairs())

        session.remove_events(lambda e: e.type == "progress")
        out = tmp_path / "stripped.jsonl"
        serialize(session, out)
        session2 = load(out)

        assert len(session2.tool_pairs()) == original_pairs

    def test_leaf_preserved(self, progress_session_events, tmp_jsonl, tmp_path):
        """The leaf event is preserved after removal."""
        path = tmp_jsonl(progress_session_events)
        session = load(path)
        original_leaf_uuid = session.events[-1].uuid

        session.remove_events(lambda e: e.type == "progress")
        out = tmp_path / "stripped.jsonl"
        serialize(session, out)
        session2 = load(out)

        assert session2.events[-1].uuid == original_leaf_uuid

    def test_real_session_strip_progress(self, dead_session_path, tmp_path):
        """Real session: strip progress + system, chain still walks."""
        session = load(dead_session_path)
        session.remove_events(lambda e: e.type in ("progress", "system"))

        out = tmp_path / "stripped.jsonl"
        serialize(session, out)
        session2 = load(out)

        chain = session2.tree.walk_chain_to_root(session2.events[-1])
        assert chain[0].parent_uuid is None
        assert len(chain) > 1

        # No dangling refs
        uuid_set = {e.uuid for e in session2.events}
        for event in session2.events:
            if event.parent_uuid is not None:
                assert event.parent_uuid in uuid_set


# ============================================================
# Microcompaction Awareness
# ============================================================


class TestMicrocompaction:
    """Recognizing already-cleared tool results."""

    def test_cleared_marker_detected(self, cleared_session_events, tmp_jsonl):
        """Standard CC placeholder detected."""
        path = tmp_jsonl(cleared_session_events)
        session = load(path)
        pair = session.tool_pairs()[0]
        assert pair.is_cleared is True
        assert pair.tool_result_block.is_cleared is True

    def test_cleared_content_size_zero(self, cleared_session_events, tmp_jsonl):
        """Cleared results report content_size=0."""
        path = tmp_jsonl(cleared_session_events)
        session = load(path)
        pair = session.tool_pairs()[0]
        assert pair.result_size == 0

    def test_normal_result_not_cleared(self, tool_session_events, tmp_jsonl):
        """Normal results are not flagged as cleared."""
        path = tmp_jsonl(tool_session_events)
        session = load(path)
        pair = session.tool_pairs()[0]
        assert pair.is_cleared is False
        assert pair.result_size > 0

    def test_file_backed_cleared(self, tmp_jsonl):
        """File-backed compaction variant detected."""
        events = [
            make_event("u1", None, "user",
                       message=make_user_message([make_text("hi")])),
            make_event("a1", "u1", "assistant",
                       message=make_assistant_message(
                           [make_tool_use("t1", "Read", {"file_path": "/x"})],
                           msg_id="msg_001", stop_reason="tool_use")),
            make_event("u2", "a1", "user",
                       message=make_user_message(
                           [make_tool_result("t1",
                                           "<compacted>Tool result saved to: /tmp/cached.txt\nOriginal tokens: 5000</compacted>")])),
        ]
        path = tmp_jsonl(events)
        session = load(path)
        assert session.tool_pairs()[0].is_cleared is True


# ============================================================
# Token Estimation
# ============================================================


class TestTokenEstimation:
    """CC-compatible token counting."""

    def test_empty(self):
        assert estimate_tokens("") == 0

    def test_short(self):
        # 12 chars -> round(12/4)=3 -> ceil(3*1.3333)=ceil(4.0)=4
        assert estimate_tokens("hello world!") == 4

    def test_medium(self):
        # 100 chars -> round(100/4)=25 -> ceil(25*1.3333)=ceil(33.33)=34
        assert estimate_tokens("x" * 100) == 34

    def test_large(self):
        # 10000 -> round(10000/4)=2500 -> ceil(2500*1.3333)=ceil(3333.25)=3334
        assert estimate_tokens("x" * 10000) == 3334

    def test_matches_cc_formula(self):
        """Verify against CC's exact JS formula for many inputs."""
        for length in [0, 1, 2, 3, 4, 5, 7, 10, 15, 50, 100, 500, 1000, 5000, 10000, 50000]:
            expected = math.ceil(round(length / 4) * 1.3333) if length > 0 else 0
            assert estimate_tokens("x" * length) == expected, f"Failed for length={length}"

    def test_content_block_tokens(self, tool_session_events, tmp_jsonl):
        """Content blocks expose token count."""
        path = tmp_jsonl(tool_session_events)
        events = list(parse(path))
        # The tool result has "def foo():\n    return 42\n" = 24 chars
        result_block = events[2].message.content[0]
        assert result_block.tokens > 0
        assert result_block.tokens == estimate_tokens("def foo():\n    return 42\n")


# ============================================================
# Real Session Tests
# ============================================================


class TestRealSession:
    """Tests against the actual dead.jsonl session data."""

    def test_event_count(self, dead_session_path):
        """Known event count from analysis."""
        session = load(dead_session_path)
        assert len(session.events) == 1645

    def test_event_type_counts(self, dead_session_path):
        """Known type distribution from analysis."""
        session = load(dead_session_path)
        by_type = session.events_by_type()
        assert len(by_type.get("progress", [])) == 923
        assert len(by_type.get("assistant", [])) == 443
        assert len(by_type.get("user", [])) == 228

    def test_tool_pair_count(self, dead_session_path):
        """Known tool result count from analysis."""
        session = load(dead_session_path)
        pairs = session.tool_pairs()
        assert len(pairs) == 192

    def test_tool_names(self, dead_session_path):
        """Known tool name distribution."""
        session = load(dead_session_path)
        from collections import Counter
        names = Counter(p.tool_name for p in session.tool_pairs())
        assert names["Bash"] == 107
        assert names["Read"] == 32

    def test_total_tokens_range(self, dead_session_path):
        """Total tokens in expected range (~195K from analysis)."""
        session = load(dead_session_path)
        tokens = session.total_tokens()
        # Our analysis showed ~195K. CC's formula gives higher estimate
        # than simple chars/4 due to 1.3333 overhead factor
        assert 100_000 < tokens < 400_000

    def test_conversation_messages_alternate(self, dead_session_path):
        """Reconstructed messages alternate roles."""
        session = load(dead_session_path)
        msgs = session.messages()
        for i in range(1, len(msgs)):
            assert msgs[i]["role"] != msgs[i - 1]["role"], \
                f"Messages {i-1} and {i} have same role: {msgs[i]['role']}"

    def test_strip_progress_and_reserialize(self, dead_session_path, tmp_path):
        """Can strip progress events and produce valid smaller session."""
        session = load(dead_session_path)
        session.remove_events(lambda e: e.type == "progress")
        assert len(session.events) == 1645 - 923

        out = tmp_path / "stripped.jsonl"
        serialize(session, out)

        # Verify output is smaller
        assert out.stat().st_size < dead_session_path.stat().st_size

        # Verify output is valid
        session2 = load(out)
        assert len(session2.events) == 1645 - 923
        assert len(session2.tool_pairs()) == 192  # Tool pairs preserved
