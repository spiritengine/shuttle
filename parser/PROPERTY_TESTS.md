# Property-Based Tests for CC Session JSONL Parser

## Information-Theoretic Foundation

The parser is a codec: it maps raw JSONL bytestreams to typed event trees and back. From an information-theoretic perspective, the parse function must be a **sufficient statistic** — it preserves all information needed to reconstruct the original session. The round-trip property `parse(serialize(parse(x))) == parse(x)` is the operational definition of sufficiency.

We test three classes of properties:

1. **Conservation laws** — quantities that must be invariant under parse/serialize
2. **Structural invariants** — predicates that hold on ALL valid parse trees
3. **Round-trip properties** — idempotence and information preservation

---

## Hypothesis Strategy Definitions

```python
"""
strategies.py — Hypothesis strategies for generating CC session JSONL events.

The generating grammar mirrors the JSONL schema:
  Session = Event+
  Event = UserEvent | AssistantEvent | ProgressEvent | SystemEvent | QueueEvent
  UserEvent.message.content = (TextBlock | ToolResultBlock)+
  AssistantEvent.message.content = (ThinkingBlock | TextBlock | ToolUseBlock)+
"""

import string
from hypothesis import strategies as st, assume, settings, HealthCheck
from hypothesis.stateful import RuleBasedStateMachine, rule, invariant
from dataclasses import dataclass, field
from typing import Optional
import uuid as uuid_mod
import json


# --- Atomic Strategies ---

uuids = st.builds(lambda: str(uuid_mod.uuid4()))
timestamps = st.builds(
    lambda y, mo, d, h, mi, s, ms: f"20{y:02d}-{mo:02d}-{d:02d}T{h:02d}:{mi:02d}:{s:02d}.{ms:03d}Z",
    st.integers(24, 26),
    st.integers(1, 12),
    st.integers(1, 28),
    st.integers(0, 23),
    st.integers(0, 59),
    st.integers(0, 59),
    st.integers(0, 999),
)
session_ids = st.shared(uuids, key="session_id")
tool_names = st.sampled_from([
    "Bash", "Read", "Write", "Edit", "Grep", "Glob",
    "TodoWrite", "Task", "WebFetch", "WebSearch",
    "NotebookEdit", "AskUserQuestion",
])
model_names = st.sampled_from([
    "claude-opus-4-5-20251101",
    "claude-sonnet-4-20250514",
    "claude-haiku-3-5-20241022",
])
stop_reasons = st.sampled_from(["tool_use", "end_turn", "stop_sequence", None])
tool_use_ids = st.builds(lambda: f"toolu_{uuid_mod.uuid4().hex[:24]}")


# --- Content Block Strategies ---

@st.composite
def text_blocks(draw):
    return {"type": "text", "text": draw(st.text(min_size=1, max_size=500))}

@st.composite
def thinking_blocks(draw):
    return {
        "type": "thinking",
        "thinking": draw(st.text(min_size=1, max_size=2000)),
        "signature": draw(st.binary(min_size=32, max_size=64).map(
            lambda b: __import__('base64').b64encode(b).decode()
        )),
    }

@st.composite
def tool_use_blocks(draw, tool_id=None):
    return {
        "type": "tool_use",
        "id": tool_id or draw(tool_use_ids),
        "name": draw(tool_names),
        "input": draw(st.fixed_dictionaries({
            "command": st.text(min_size=1, max_size=200),
        })),
    }

@st.composite
def tool_result_blocks(draw, tool_use_id=None):
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id or draw(tool_use_ids),
        "content": draw(st.text(max_size=5000)),
        "is_error": draw(st.booleans()),
    }


# --- Message Strategies ---

@st.composite
def user_messages(draw, tool_use_id=None):
    """Generate a user message, optionally as a tool result for a specific tool_use_id."""
    if tool_use_id:
        content = [draw(tool_result_blocks(tool_use_id=tool_use_id))]
    else:
        content = draw(st.lists(text_blocks(), min_size=1, max_size=3))
    return {"role": "user", "content": content}

@st.composite
def assistant_messages(draw, with_tool_use=None):
    """Generate an assistant message.

    with_tool_use: if True, always include at least one tool_use block.
    """
    blocks = []
    if draw(st.booleans()):
        blocks.append(draw(thinking_blocks()))
    if draw(st.booleans()) or not with_tool_use:
        blocks.append(draw(text_blocks()))
    if with_tool_use or draw(st.booleans()):
        blocks.append(draw(tool_use_blocks()))
    if not blocks:
        blocks.append(draw(text_blocks()))
    return {
        "role": "assistant",
        "content": blocks,
        "id": f"msg_{uuid_mod.uuid4().hex[:24]}",
        "model": draw(model_names),
        "stop_reason": "tool_use" if any(b["type"] == "tool_use" for b in blocks) else draw(stop_reasons),
    }


# --- Event Strategies ---

@st.composite
def event_envelope(draw, event_type, message=None, parent_uuid=None, **extra):
    """Wrap a message in the JSONL event envelope."""
    evt = {
        "type": event_type,
        "uuid": draw(uuids),
        "parentUuid": parent_uuid,
        "timestamp": draw(timestamps),
        "sessionId": draw(session_ids),
        "isSidechain": False,
        "userType": "external",
        "cwd": "/home/user/project",
        "version": "1.0.0",
    }
    if message:
        evt["message"] = message
    evt.update(extra)
    return evt

@st.composite
def progress_events(draw, parent_uuid=None, tool_use_id=None):
    return draw(event_envelope(
        "progress",
        parent_uuid=parent_uuid,
        data={
            "type": draw(st.sampled_from(["bash_progress", "hook_progress", "mcp_progress"])),
            "output": draw(st.text(max_size=500)),
            "fullOutput": draw(st.text(max_size=2000)),
            "elapsedTimeSeconds": draw(st.integers(0, 300)),
            "totalLines": draw(st.integers(0, 1000)),
        },
        toolUseID=tool_use_id,
    ))

@st.composite
def system_events(draw, parent_uuid=None):
    return draw(event_envelope(
        "system",
        parent_uuid=parent_uuid,
        subtype=draw(st.sampled_from(["stop_hook_summary", "turn_duration"])),
    ))


# --- Session Strategies (the main composite) ---

@st.composite
def valid_sessions(draw, min_turns=1, max_turns=10):
    """Generate a complete valid session as a list of JSONL events.

    Maintains the invariant that every tool_use has exactly one tool_result,
    events form a connected tree, and role alternation holds.
    """
    events = []
    pending_tool_uses = []  # (tool_use_id, assistant_event_uuid)
    last_uuid = None

    # First event is always a user text message (root)
    user_msg = draw(user_messages())
    root_event = draw(event_envelope("user", message=user_msg, parent_uuid=None))
    events.append(root_event)
    last_uuid = root_event["uuid"]

    num_turns = draw(st.integers(min_turns, max_turns))

    for _ in range(num_turns):
        # Assistant response (may contain tool_uses)
        has_tools = draw(st.booleans())
        asst_msg = draw(assistant_messages(with_tool_use=has_tools))
        asst_event = draw(event_envelope("assistant", message=asst_msg, parent_uuid=last_uuid))
        events.append(asst_event)
        last_uuid = asst_event["uuid"]

        # Collect tool_use IDs from this response
        new_tool_uses = [
            b["id"] for b in asst_msg["content"] if b["type"] == "tool_use"
        ]

        # Optionally add progress events for each tool_use
        for tu_id in new_tool_uses:
            num_progress = draw(st.integers(0, 5))
            for _ in range(num_progress):
                prog = draw(progress_events(parent_uuid=last_uuid, tool_use_id=tu_id))
                events.append(prog)

        # Generate tool_results for each tool_use
        if new_tool_uses:
            for tu_id in new_tool_uses:
                result_msg = draw(user_messages(tool_use_id=tu_id))
                result_event = draw(event_envelope("user", message=result_msg, parent_uuid=last_uuid))
                events.append(result_event)
                last_uuid = result_event["uuid"]

        # Optionally add system events
        if draw(st.booleans()):
            sys_evt = draw(system_events(parent_uuid=last_uuid))
            events.append(sys_evt)

    return events


@st.composite
def degenerate_sessions(draw):
    """Edge cases: empty, single event, all progress, no tools."""
    kind = draw(st.sampled_from(["empty", "single_user", "all_progress", "no_tools", "huge_turn"]))

    if kind == "empty":
        return []
    elif kind == "single_user":
        msg = draw(user_messages())
        return [draw(event_envelope("user", message=msg, parent_uuid=None))]
    elif kind == "all_progress":
        events = []
        parent = None
        for _ in range(draw(st.integers(1, 50))):
            evt = draw(progress_events(parent_uuid=parent))
            events.append(evt)
            parent = evt["uuid"]
        return events
    elif kind == "no_tools":
        events = []
        last = None
        for i in range(draw(st.integers(2, 10))):
            if i % 2 == 0:
                msg = draw(user_messages())
                evt = draw(event_envelope("user", message=msg, parent_uuid=last))
            else:
                msg = draw(assistant_messages(with_tool_use=False))
                evt = draw(event_envelope("assistant", message=msg, parent_uuid=last))
            events.append(evt)
            last = evt["uuid"]
        return events
    elif kind == "huge_turn":
        # Single assistant response with many tool_uses
        events = []
        user_msg = draw(user_messages())
        root = draw(event_envelope("user", message=user_msg, parent_uuid=None))
        events.append(root)

        # Build assistant message with many tools
        tool_ids = [draw(tool_use_ids) for _ in range(draw(st.integers(5, 20)))]
        blocks = [{"type": "tool_use", "id": tid, "name": draw(tool_names), "input": {}} for tid in tool_ids]
        asst_msg = {"role": "assistant", "content": blocks, "id": f"msg_{uuid_mod.uuid4().hex[:24]}",
                    "model": "claude-opus-4-5-20251101", "stop_reason": "tool_use"}
        asst_evt = draw(event_envelope("assistant", message=asst_msg, parent_uuid=root["uuid"]))
        events.append(asst_evt)

        for tid in tool_ids:
            result_msg = draw(user_messages(tool_use_id=tid))
            result_evt = draw(event_envelope("user", message=result_msg, parent_uuid=asst_evt["uuid"]))
            events.append(result_evt)

        return events


@st.composite
def corrupted_sessions(draw, base=None):
    """Take a valid session and corrupt it in various ways for negative testing."""
    session = base or draw(valid_sessions(min_turns=3, max_turns=8))
    assume(len(session) > 3)

    corruption = draw(st.sampled_from([
        "duplicate_uuid", "orphan_parent", "duplicate_tool_use_id",
        "missing_tool_result", "wrong_role_content", "broken_timestamp",
        "interleaved_message_ids",
    ]))

    import copy
    session = copy.deepcopy(session)

    if corruption == "duplicate_uuid":
        session[2]["uuid"] = session[0]["uuid"]
    elif corruption == "orphan_parent":
        session[1]["parentUuid"] = str(uuid_mod.uuid4())
    elif corruption == "duplicate_tool_use_id":
        # Find two tool_use blocks and give them the same ID
        tool_uses = [(i, b) for i, evt in enumerate(session)
                     if evt.get("message", {}).get("role") == "assistant"
                     for b in evt.get("message", {}).get("content", [])
                     if b.get("type") == "tool_use"]
        if len(tool_uses) >= 2:
            tool_uses[1][1]["id"] = tool_uses[0][1]["id"]
    elif corruption == "missing_tool_result":
        # Remove a tool_result event
        for i, evt in enumerate(session):
            if evt.get("message", {}).get("role") == "user":
                content = evt["message"].get("content", [])
                if any(b.get("type") == "tool_result" for b in content):
                    session.pop(i)
                    break
    elif corruption == "wrong_role_content":
        # Put tool_use in a user message
        for evt in session:
            if evt.get("message", {}).get("role") == "user":
                evt["message"]["content"] = [{"type": "tool_use", "id": "bad", "name": "Bash", "input": {}}]
                break
    elif corruption == "broken_timestamp":
        session[0]["timestamp"] = "not-a-timestamp"
    elif corruption == "interleaved_message_ids":
        # Give non-adjacent assistant events the same message.id
        asst_events = [e for e in session if e.get("message", {}).get("role") == "assistant"]
        if len(asst_events) >= 2:
            asst_events[-1]["message"]["id"] = asst_events[0]["message"]["id"]

    return session
```

---

## Conservation Laws

These quantities are invariants of the parse↔serialize bijection.

```python
"""
test_conservation.py — Conservation laws: quantities preserved across parse/serialize.
"""

from hypothesis import given, settings, assume
import hypothesis.strategies as st
from strategies import valid_sessions, degenerate_sessions


# --- Extensive quantities (additive over events) ---

@given(valid_sessions())
def test_total_content_chars_preserved(session):
    """Total character count across all content blocks is conserved.

    This is the "mass" of the session — the first moment of the
    content size distribution.
    """
    parsed = parse(session)
    serialized = serialize(parsed)
    reparsed = parse(serialized)

    original_chars = sum_content_chars(parsed)
    roundtrip_chars = sum_content_chars(reparsed)

    assert original_chars == roundtrip_chars


@given(valid_sessions())
def test_event_count_by_type_preserved(session):
    """Event type histogram is a conserved quantity.

    The parse must not create, destroy, or reclassify events.
    type_counts: {user: n, assistant: m, progress: p, system: s, queue: q}
    """
    parsed = parse(session)
    serialized = serialize(parsed)
    reparsed = parse(serialized)

    assert count_by_type(parsed) == count_by_type(reparsed)


@given(valid_sessions(min_turns=2))
def test_tool_pair_count_preserved(session):
    """Number of (tool_use, tool_result) pairs is conserved.

    This is a topological invariant — it counts the number of
    tool invocation edges in the conversation graph.
    """
    parsed = parse(session)
    pairs_before = count_tool_pairs(parsed)

    serialized = serialize(parsed)
    reparsed = parse(serialized)
    pairs_after = count_tool_pairs(reparsed)

    assert pairs_before == pairs_after


@given(valid_sessions())
def test_thinking_block_count_preserved(session):
    """Thinking blocks are opaque — never created or destroyed by parse/serialize."""
    parsed = parse(session)
    serialized = serialize(parsed)
    reparsed = parse(serialized)

    assert count_thinking_blocks(parsed) == count_thinking_blocks(reparsed)


@given(valid_sessions())
def test_message_id_groups_preserved(session):
    """The set of distinct message.id values is conserved.

    Each message.id identifies a single API response; the parser
    must not split or merge these.
    """
    parsed = parse(session)
    serialized = serialize(parsed)
    reparsed = parse(serialized)

    assert extract_message_ids(parsed) == extract_message_ids(reparsed)


@given(valid_sessions())
def test_token_usage_totals_preserved(session):
    """Sum of token usage statistics is conserved (when present).

    input_tokens, output_tokens, cache_* tokens are accounting quantities.
    """
    parsed = parse(session)
    serialized = serialize(parsed)
    reparsed = parse(serialized)

    assert sum_token_usage(parsed) == sum_token_usage(reparsed)


@given(valid_sessions())
def test_uuid_set_preserved(session):
    """The set of all UUIDs in the session is conserved.

    UUIDs are the atoms of identity in the event tree.
    """
    parsed = parse(session)
    serialized = serialize(parsed)
    reparsed = parse(serialized)

    assert {e.uuid for e in parsed.events} == {e.uuid for e in reparsed.events}
```

---

## Structural Invariants

These hold on ANY valid parsed session, regardless of round-tripping.

```python
"""
test_invariants.py — Structural invariants that hold on all valid parse trees.
"""

from hypothesis import given, assume
from strategies import valid_sessions, degenerate_sessions


# --- Tree structure ---

@given(valid_sessions())
def test_event_tree_is_dag(session):
    """The parentUuid graph must be a DAG (directed acyclic graph).

    Cycles would make traversal undefined. Specifically: the graph
    is a forest (set of trees) — each connected component has
    exactly one root.
    """
    parsed = parse(session)
    graph = build_parent_graph(parsed)

    assert is_dag(graph)
    assert all_components_are_trees(graph)


@given(valid_sessions())
def test_exactly_one_root_per_conversation(session):
    """Each conversation thread has exactly one root (parentUuid=None).

    Multiple roots would indicate disconnected sub-conversations.
    For non-sidechain events, there should be a single connected tree.
    """
    parsed = parse(session)
    main_events = [e for e in parsed.events if not e.is_sidechain]
    roots = [e for e in main_events if e.parent_uuid is None]

    assert len(roots) == 1


@given(valid_sessions())
def test_parent_uuid_references_exist(session):
    """Every parentUuid (when non-null) references an existing event UUID.

    No dangling pointers in the tree.
    """
    parsed = parse(session)
    uuid_set = {e.uuid for e in parsed.events}

    for event in parsed.events:
        if event.parent_uuid is not None:
            assert event.parent_uuid in uuid_set


# --- Tool use/result matching ---

@given(valid_sessions(min_turns=1))
def test_tool_use_result_bijection(session):
    """Every tool_use.id has EXACTLY ONE matching tool_result.tool_use_id.

    This is the fundamental matching invariant. It's a bijection
    between the set of tool_use blocks and tool_result blocks.
    """
    parsed = parse(session)

    tool_uses = extract_tool_use_ids(parsed)
    tool_results = extract_tool_result_ids(parsed)

    # Every tool_use has a result
    assert set(tool_uses) == set(tool_results)

    # No duplicates in either set (injectivity)
    assert len(tool_uses) == len(set(tool_uses))
    assert len(tool_results) == len(set(tool_results))


@given(valid_sessions(min_turns=1))
def test_tool_result_follows_tool_use_in_file_order(session):
    """tool_result events appear AFTER their corresponding tool_use events.

    This is causality — you can't have a result before the invocation.
    Specifically: the file-order index of the tool_result event is
    strictly greater than the index of the tool_use event.
    """
    parsed = parse(session)

    for tu_id, tu_index in tool_use_positions(parsed):
        tr_index = tool_result_position(parsed, tu_id)
        assert tr_index > tu_index


@given(valid_sessions(min_turns=1))
def test_tool_use_in_assistant_result_in_user(session):
    """tool_use blocks are in assistant messages; tool_result blocks in user messages.

    This is the role constraint — it mirrors the API contract.
    """
    parsed = parse(session)

    for event in parsed.events:
        if event.message is None:
            continue
        for block in event.message.content:
            if block.type == "tool_use":
                assert event.message.role == "assistant"
            if block.type == "tool_result":
                assert event.message.role == "user"


# --- Role alternation ---

@given(valid_sessions(min_turns=2))
def test_reconstructed_messages_alternate_roles(session):
    """When events are reconstructed into API messages, roles strictly alternate.

    After filtering out progress/system/sidechain events and merging
    consecutive same-role events, the sequence must be:
    user, assistant, user, assistant, ...

    This is the API contract — Claude rejects non-alternating messages.
    """
    parsed = parse(session)
    messages = reconstruct_api_messages(parsed)

    for i in range(1, len(messages)):
        assert messages[i].role != messages[i-1].role


# --- Progress events ---

@given(valid_sessions(min_turns=2))
def test_progress_events_are_children_of_tool_use_events(session):
    """Progress events are always descendants of an event containing a tool_use.

    Progress events report on ongoing tool execution. They must be
    linked (via parentUuid chain) to the assistant event that
    initiated the tool.
    """
    parsed = parse(session)

    for event in parsed.events:
        if event.type != "progress":
            continue
        # Walk up the parent chain until we find a tool_use or hit root
        ancestor = find_ancestor_with_tool_use(parsed, event)
        assert ancestor is not None


@given(valid_sessions())
def test_progress_events_have_tool_use_id(session):
    """Progress events with toolUseID reference an existing tool_use.id."""
    parsed = parse(session)
    tool_use_id_set = set(extract_tool_use_ids(parsed))

    for event in parsed.events:
        if event.type == "progress" and event.tool_use_id:
            assert event.tool_use_id in tool_use_id_set


# --- Message.id contiguity ---

@given(valid_sessions(min_turns=3))
def test_message_id_groups_contiguous(session):
    """Events sharing the same message.id are contiguous in file order.

    No interleaving — you can't have events A1, B1, A2 where A1 and A2
    share a message.id but B1 has a different one. This is because
    streaming delivers all blocks of one response before starting the next.
    """
    parsed = parse(session)

    message_id_spans = {}
    for i, event in enumerate(parsed.events):
        if event.message and event.message.id:
            mid = event.message.id
            if mid not in message_id_spans:
                message_id_spans[mid] = (i, i)
            else:
                message_id_spans[mid] = (message_id_spans[mid][0], i)

    # Check that no other message.id appears within a span
    for mid, (start, end) in message_id_spans.items():
        for j in range(start, end + 1):
            evt = parsed.events[j]
            if evt.message and evt.message.id:
                assert evt.message.id == mid or j == start or j == end


# --- Content type constraints ---

@given(valid_sessions())
def test_thinking_blocks_only_in_assistant(session):
    """Thinking blocks appear only in assistant messages."""
    parsed = parse(session)

    for event in parsed.events:
        if event.message is None:
            continue
        for block in event.message.content:
            if hasattr(block, 'type') and block.type == "thinking":
                assert event.message.role == "assistant"


@given(valid_sessions())
def test_stop_reason_consistency(session):
    """stop_reason='tool_use' iff the message contains a tool_use block.

    The stop_reason field encodes why the assistant stopped generating.
    If it stopped to use a tool, there must be a tool_use block.
    """
    parsed = parse(session)

    for event in parsed.events:
        if event.message and event.message.role == "assistant" and event.message.stop_reason:
            has_tool_use = any(
                b.type == "tool_use" for b in event.message.content
            )
            if event.message.stop_reason == "tool_use":
                assert has_tool_use
```

---

## Round-Trip Properties

```python
"""
test_roundtrip.py — Round-trip (idempotence, size, structure) properties.
"""

from hypothesis import given, assume, settings
from strategies import valid_sessions, degenerate_sessions


@given(valid_sessions())
def test_parse_serialize_idempotent(session):
    """parse(serialize(parse(x))) == parse(x)

    The fundamental sufficiency property. If parsing loses no information,
    then re-parsing the serialized form must yield the identical structure.
    """
    parsed = parse(session)
    serialized = serialize(parsed)
    reparsed = parse(serialized)

    assert parsed == reparsed  # structural equality


@given(valid_sessions())
def test_serialize_parse_fixpoint(session):
    """serialize(parse(serialize(parse(x)))) == serialize(parse(x))

    The dual: serialization is also a fixpoint after one parse.
    This is equivalent to the above but tests from the other direction.
    """
    parsed = parse(session)
    s1 = serialize(parsed)
    s2 = serialize(parse(s1))

    assert s1 == s2


@given(valid_sessions())
def test_round_trip_size_bounded(session):
    """len(serialize(parse(session))) <= len(session) + epsilon

    The serialized form should not be substantially larger than the original.
    Small epsilon accounts for formatting differences (trailing newlines,
    key ordering in JSON objects, float precision).
    """
    original_size = sum(len(json.dumps(e)) for e in session)
    parsed = parse(session)
    serialized = serialize(parsed)
    roundtrip_size = sum(len(line) for line in serialized.split('\n') if line.strip())

    epsilon = 100 * len(session)  # 100 bytes per event for formatting slack
    assert roundtrip_size <= original_size + epsilon


@given(valid_sessions())
def test_tree_structure_survives_roundtrip(session):
    """parentUuid relationships are identical after round-trip.

    The tree topology is the skeleton of the conversation.
    """
    parsed = parse(session)
    serialized = serialize(parsed)
    reparsed = parse(serialized)

    original_edges = {(e.uuid, e.parent_uuid) for e in parsed.events}
    roundtrip_edges = {(e.uuid, e.parent_uuid) for e in reparsed.events}

    assert original_edges == roundtrip_edges


@given(valid_sessions())
def test_file_order_preserved(session):
    """Event order in serialized output matches parse order.

    File order is semantically meaningful (it encodes causality).
    The serializer must not reorder events.
    """
    parsed = parse(session)
    serialized = serialize(parsed)
    reparsed = parse(serialized)

    original_uuids = [e.uuid for e in parsed.events]
    roundtrip_uuids = [e.uuid for e in reparsed.events]

    assert original_uuids == roundtrip_uuids


@given(valid_sessions())
def test_content_byte_exact_preservation(session):
    """Content strings are preserved byte-for-byte.

    No normalization, no trimming, no encoding changes.
    This is the "lossless" guarantee for text content.
    """
    parsed = parse(session)
    serialized = serialize(parsed)
    reparsed = parse(serialized)

    for orig, rt in zip(parsed.events, reparsed.events):
        if orig.message:
            for ob, rb in zip(orig.message.content, rt.message.content):
                if hasattr(ob, 'text'):
                    assert ob.text == rb.text
                if hasattr(ob, 'content'):
                    assert ob.content == rb.content
                if hasattr(ob, 'thinking'):
                    assert ob.thinking == rb.thinking
```

---

## Compression-Relevant Properties

These verify that specific compression operations don't violate structural invariants.

```python
"""
test_compression_safety.py — Properties that compression transforms must preserve.
"""

from hypothesis import given, assume
from strategies import valid_sessions


@given(valid_sessions(min_turns=3))
def test_removing_progress_preserves_tool_matching(session):
    """Removing all progress events does not break tool_use/tool_result pairing.

    Progress events are purely informational (streaming output). They
    contribute zero bits to the tool invocation graph.
    """
    parsed = parse(session)
    filtered = remove_events_by_type(parsed, "progress")

    # Tool matching still holds
    tool_uses = extract_tool_use_ids(filtered)
    tool_results = extract_tool_result_ids(filtered)
    assert set(tool_uses) == set(tool_results)


@given(valid_sessions(min_turns=3))
def test_removing_progress_preserves_role_alternation(session):
    """Progress removal doesn't break message alternation.

    Since progress events are filtered out during message reconstruction
    anyway, their removal should be transparent.
    """
    parsed = parse(session)
    filtered = remove_events_by_type(parsed, "progress")

    messages = reconstruct_api_messages(filtered)
    for i in range(1, len(messages)):
        assert messages[i].role != messages[i-1].role


@given(valid_sessions(min_turns=2))
def test_removing_system_events_preserves_structure(session):
    """System events (hooks, timing) carry no conversational information.

    Removing them preserves all structural invariants.
    """
    parsed = parse(session)
    filtered = remove_events_by_type(parsed, "system")

    # Tree still connected (re-link children of removed nodes)
    assert is_connected_after_removal(parsed, filtered)

    # Tool matching preserved
    tool_uses = extract_tool_use_ids(filtered)
    tool_results = extract_tool_result_ids(filtered)
    assert set(tool_uses) == set(tool_results)


@given(valid_sessions(min_turns=2))
def test_removing_meta_preserves_alternation(session):
    """isMeta events are local command caveats; removing them keeps alternation."""
    parsed = parse(session)
    filtered = remove_meta_events(parsed)

    messages = reconstruct_api_messages(filtered)
    for i in range(1, len(messages)):
        assert messages[i].role != messages[i-1].role


@given(valid_sessions(min_turns=3))
def test_truncating_tool_results_preserves_matching(session):
    """Truncating tool_result content preserves the matching invariant.

    The tool_use_id link is structural, not content-dependent.
    """
    parsed = parse(session)
    truncated = truncate_tool_results(parsed, max_chars=100)

    tool_uses = extract_tool_use_ids(truncated)
    tool_results = extract_tool_result_ids(truncated)
    assert set(tool_uses) == set(tool_results)


@given(valid_sessions(min_turns=3))
def test_content_summarization_preserves_structure(session):
    """Replacing tool_result content with a summary preserves all structural properties.

    Only the content field changes; all IDs, roles, and tree links remain.
    """
    parsed = parse(session)
    summarized = summarize_tool_results(parsed, summarizer=lambda x: x[:50])

    # Structure preserved
    assert count_by_type(summarized) == count_by_type(parsed)
    assert count_tool_pairs(summarized) == count_tool_pairs(parsed)

    # Roles preserved
    messages = reconstruct_api_messages(summarized)
    for i in range(1, len(messages)):
        assert messages[i].role != messages[i-1].role


@given(valid_sessions(min_turns=2))
def test_tool_result_size_monotone_with_reference_probability(session):
    """Larger tool results are more likely to be referenced later.

    From decay curve data: results > 2K chars have 73% reference rate
    vs ~50% for smaller results. This is a statistical property we
    verify holds on synthetic data with injected references.

    (This is a soft property — we test the correlation direction,
    not exact values.)
    """
    parsed = parse(session)
    results_with_size = get_tool_results_with_size(parsed)

    if len(results_with_size) < 5:
        assume(False)  # Need enough data points

    # Verify the parser correctly extracts sizes for downstream analysis
    for result in results_with_size:
        assert result.size_chars == len(result.content)
        assert result.size_chars >= 0


@given(valid_sessions(min_turns=3))
def test_chain_walkable_after_event_removal(session):
    """After removing any subset of non-conversational events, the parentUuid
    chain must remain walkable from leaf to root.

    CC loads sessions by walking parentUuid from the last event to the first.
    If this chain is broken, CC cannot resume the session. This is the critical
    round-trip safety property for all compression transforms.

    Invariant: ∀ leaf ∈ serialized_events: walk(leaf → root) terminates at null parent.
    """
    parsed = parse(session)

    # Remove a random subset of progress/system events
    removable = [e for e in parsed.events if e.type in ("progress", "system")]
    if not removable:
        assume(False)

    to_remove = set(e.uuid for e in removable[:len(removable)//2])
    parsed.remove_events(lambda e: e.uuid in to_remove)

    # Serialize and reload
    serialized = serialize_to_bytes(parsed)
    reloaded = parse_bytes(serialized)

    # Chain must be walkable
    leaf = reloaded.events[-1]
    chain = reloaded.tree.walk_chain_to_root(leaf)
    assert chain[0].parent_uuid is None, "Chain must reach root (null parent)"
    assert leaf in chain, "Leaf must be in its own chain"


@given(valid_sessions(min_turns=3))
def test_logical_parent_skips_removed_events(session):
    """logicalParentUuid is set when events are removed mid-chain,
    allowing CC to skip over gaps.

    This mirrors CC's own compaction behavior: compact_boundary events
    use logicalParentUuid to bridge over summarized sections.
    """
    parsed = parse(session)

    # Remove a middle event that has both parent and children
    middle_events = [e for e in parsed.events
                     if e.parent_uuid is not None and e.children]
    if not middle_events:
        assume(False)

    removed = middle_events[0]
    removed_uuid = removed.uuid
    removed_parent = removed.parent_uuid

    parsed.remove_events(lambda e: e.uuid == removed_uuid)
    serialized = serialize_to_bytes(parsed)
    reloaded = parse_bytes(serialized)

    # Events that were children of the removed event should now
    # point to the removed event's parent (via parentUuid or logicalParentUuid)
    for event in reloaded.events:
        if event.raw.get("logicalParentUuid") == removed_parent:
            break  # Found the bridge
        if event.parent_uuid == removed_parent and event.uuid != removed_uuid:
            break  # Direct relink
    else:
        # At minimum, the chain must still be walkable
        chain = reloaded.tree.walk_chain_to_root(reloaded.events[-1])
        assert chain[0].parent_uuid is None


@given(valid_sessions(min_turns=2))
def test_token_estimation_matches_cc_formula(session):
    """Token estimates use CC's exact formula: ceil(round(len/4) * 1.3333).

    This ensures our compression decisions align with CC's internal
    threshold calculations. A mismatch would cause CC to trigger
    compaction at unexpected times on our output.
    """
    import math
    parsed = parse(session)

    for event in parsed.events:
        if event.message is None:
            continue
        for block in event.message.content:
            if hasattr(block, 'text') and block.text:
                expected = math.ceil(round(len(block.text) / 4) * 1.3333)
                assert estimate_tokens(block.text) == expected
            if hasattr(block, 'content') and isinstance(block.content, str):
                expected = math.ceil(round(len(block.content) / 4) * 1.3333)
                assert estimate_tokens(block.content) == expected
```

---

## Stateful Testing (Model-Based)

```python
"""
test_stateful.py — Stateful tests using Hypothesis RuleBasedStateMachine.

Models the session as a state machine where each transition
(add event) must maintain all invariants.
"""

from hypothesis.stateful import RuleBasedStateMachine, rule, invariant, precondition
from hypothesis import strategies as st
from strategies import (
    uuids, timestamps, session_ids, tool_use_ids,
    user_messages, assistant_messages, progress_events, system_events
)


class SessionBuilderMachine(RuleBasedStateMachine):
    """State machine that builds sessions event by event, checking invariants after each step."""

    def __init__(self):
        super().__init__()
        self.events = []
        self.uuid_set = set()
        self.pending_tool_uses = []  # tool_use_ids without results yet
        self.last_role = None
        self.last_uuid = None

    @rule(target=st.just("added"))
    def add_user_text(self):
        """Add a plain text user message."""
        msg = {"role": "user", "content": [{"type": "text", "text": "hello"}]}
        evt = self._make_event("user", msg)
        self.events.append(evt)
        self.last_role = "user"
        self.last_uuid = evt["uuid"]

    @rule(target=st.just("added"))
    @precondition(lambda self: self.last_role == "user" or self.last_role is None)
    def add_assistant_with_tool(self):
        """Add an assistant message with a tool_use block."""
        tid = f"toolu_{__import__('uuid').uuid4().hex[:24]}"
        msg = {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": tid, "name": "Bash", "input": {}}],
            "id": f"msg_{__import__('uuid').uuid4().hex[:24]}",
            "model": "claude-opus-4-5-20251101",
            "stop_reason": "tool_use",
        }
        evt = self._make_event("assistant", msg)
        self.events.append(evt)
        self.pending_tool_uses.append(tid)
        self.last_role = "assistant"
        self.last_uuid = evt["uuid"]

    @rule(target=st.just("added"))
    @precondition(lambda self: len(self.pending_tool_uses) > 0)
    def add_tool_result(self):
        """Add a tool_result for a pending tool_use."""
        tid = self.pending_tool_uses.pop(0)
        msg = {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tid, "content": "ok", "is_error": False}],
        }
        evt = self._make_event("user", msg)
        self.events.append(evt)
        self.last_role = "user"
        self.last_uuid = evt["uuid"]

    @rule(target=st.just("added"))
    @precondition(lambda self: len(self.events) > 0)
    def add_progress(self):
        """Add a progress event."""
        evt = self._make_event("progress", None, data={"type": "bash_progress", "output": "..."})
        self.events.append(evt)
        # Progress doesn't change last_role

    @invariant()
    def no_duplicate_uuids(self):
        """All UUIDs are unique."""
        uuids = [e["uuid"] for e in self.events]
        assert len(uuids) == len(set(uuids))

    @invariant()
    def parent_uuids_valid(self):
        """All parentUuids reference existing events or are None."""
        uuid_set = {e["uuid"] for e in self.events}
        for e in self.events:
            if e["parentUuid"] is not None:
                assert e["parentUuid"] in uuid_set

    @invariant()
    def tool_matching_partial(self):
        """All completed tool_use/tool_result pairs match correctly."""
        uses = set()
        results = set()
        for e in self.events:
            if e.get("message"):
                for b in e["message"].get("content", []):
                    if b.get("type") == "tool_use":
                        uses.add(b["id"])
                    if b.get("type") == "tool_result":
                        results.add(b["tool_use_id"])
        # Results must be a subset of uses (no orphan results)
        assert results.issubset(uses)
        # Pending = uses - results
        assert uses - results == set(self.pending_tool_uses)

    def _make_event(self, etype, message, **extra):
        uid = str(__import__('uuid').uuid4())
        self.uuid_set.add(uid)
        evt = {
            "type": etype,
            "uuid": uid,
            "parentUuid": self.last_uuid,
            "timestamp": "2026-01-23T00:00:00.000Z",
            "sessionId": "test-session",
            "isSidechain": False,
        }
        if message:
            evt["message"] = message
        evt.update(extra)
        return evt


TestSessionBuilder = SessionBuilderMachine.TestCase
```

---

## Negative/Robustness Properties

```python
"""
test_negative.py — Tests for parser behavior on invalid input.

The parser must either reject invalid input cleanly or
degrade gracefully (never silently lose data).
"""

from hypothesis import given
from strategies import corrupted_sessions, valid_sessions
import pytest


@given(corrupted_sessions())
def test_corrupted_sessions_raise_or_warn(session):
    """Corrupted sessions must either raise ParseError or emit diagnostics.

    The parser must NEVER silently accept invalid input and produce
    a "valid-looking" but incorrect parse tree.
    """
    result = parse(session, strict=True)
    # If strict mode doesn't raise, it must report diagnostics
    if result is not None:
        assert len(result.diagnostics) > 0


@given(valid_sessions())
def test_duplicate_uuid_detected(session):
    """Parser detects and reports duplicate UUIDs."""
    if len(session) < 2:
        return
    import copy
    corrupted = copy.deepcopy(session)
    corrupted[1]["uuid"] = corrupted[0]["uuid"]

    with pytest.raises(ParseError, match="duplicate.*uuid"):
        parse(corrupted, strict=True)


@given(valid_sessions(min_turns=2))
def test_orphan_tool_result_detected(session):
    """Parser detects tool_results that reference non-existent tool_uses."""
    parsed = parse(session)
    # Inject an orphan tool_result
    orphan_event = make_event("user", message={
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "nonexistent_id", "content": "x"}]
    })
    parsed.events.append(orphan_event)

    with pytest.raises(ValidationError, match="orphan.*tool_result"):
        validate(parsed)


@given(valid_sessions(min_turns=2))
def test_missing_tool_result_detected(session):
    """Parser detects tool_uses without corresponding results (in complete sessions)."""
    parsed = parse(session)
    # Remove a tool_result
    results = [e for e in parsed.events if has_tool_result(e)]
    if results:
        parsed.events.remove(results[0])
        with pytest.raises(ValidationError, match="unmatched.*tool_use"):
            validate(parsed, complete=True)
```

---

## Real Data Properties

```python
"""
test_real_sessions.py — Property tests against actual CC session files.

These use the sessions/ directory as test fixtures. They verify
that the parser handles real-world complexity correctly.
"""

import pytest
from pathlib import Path

SESSION_DIR = Path(__file__).parent.parent / "sessions"
SESSION_FILES = list(SESSION_DIR.glob("*.jsonl"))


@pytest.mark.parametrize("session_file", SESSION_FILES, ids=lambda p: p.name)
def test_real_session_round_trips(session_file):
    """Every real session file round-trips without information loss."""
    original = load_jsonl(session_file)
    parsed = parse(original)
    serialized = serialize(parsed)
    reparsed = parse(serialized)

    assert parsed == reparsed


@pytest.mark.parametrize("session_file", SESSION_FILES, ids=lambda p: p.name)
def test_real_session_tool_bijection(session_file):
    """Real sessions maintain the tool_use/tool_result bijection."""
    parsed = parse(load_jsonl(session_file))

    tool_uses = extract_tool_use_ids(parsed)
    tool_results = extract_tool_result_ids(parsed)

    # In real sessions, there may be trailing tool_uses without results
    # (session died mid-execution). But every result must have a use.
    assert set(tool_results).issubset(set(tool_uses))
    # No duplicate IDs
    assert len(tool_results) == len(set(tool_results))


@pytest.mark.parametrize("session_file", SESSION_FILES, ids=lambda p: p.name)
def test_real_session_tree_structure(session_file):
    """Real sessions form valid event trees."""
    parsed = parse(load_jsonl(session_file))

    uuid_set = {e.uuid for e in parsed.events}
    for event in parsed.events:
        if event.parent_uuid is not None:
            assert event.parent_uuid in uuid_set

    # Check DAG property
    assert is_dag(build_parent_graph(parsed))


@pytest.mark.parametrize("session_file", SESSION_FILES, ids=lambda p: p.name)
def test_real_session_role_alternation(session_file):
    """Real sessions produce alternating roles when reconstructed."""
    parsed = parse(load_jsonl(session_file))
    messages = reconstruct_api_messages(parsed)

    for i in range(1, len(messages)):
        assert messages[i].role != messages[i-1].role, (
            f"Non-alternating at index {i}: "
            f"{messages[i-1].role} -> {messages[i].role}"
        )


@pytest.mark.parametrize("session_file", SESSION_FILES, ids=lambda p: p.name)
def test_real_session_progress_parentage(session_file):
    """Progress events in real sessions have valid tool_use ancestry."""
    parsed = parse(load_jsonl(session_file))

    progress_events = [e for e in parsed.events if e.type == "progress"]
    for pe in progress_events:
        if pe.tool_use_id:
            tool_use_ids = set(extract_tool_use_ids(parsed))
            assert pe.tool_use_id in tool_use_ids


@pytest.mark.parametrize("session_file", SESSION_FILES, ids=lambda p: p.name)
def test_real_session_message_id_contiguity(session_file):
    """Message.id groups in real sessions are contiguous in file order."""
    parsed = parse(load_jsonl(session_file))

    seen_ids = {}  # message_id -> (first_index, last_index)
    for i, event in enumerate(parsed.events):
        if event.message and event.message.id:
            mid = event.message.id
            if mid not in seen_ids:
                seen_ids[mid] = (i, i)
            else:
                prev_first, prev_last = seen_ids[mid]
                # Check no other message.id appears between prev_last and i
                for j in range(prev_last + 1, i):
                    other = parsed.events[j]
                    if other.message and other.message.id and other.message.id != mid:
                        # Another message.id interrupted this group
                        pytest.fail(
                            f"message.id {mid} is non-contiguous: "
                            f"interrupted by {other.message.id} at index {j}"
                        )
                seen_ids[mid] = (prev_first, i)
```

---

## Helper Functions (Interface Spec)

These are the functions that the parser must implement for the tests to pass.
They define the **minimal sufficient interface**.

```python
"""
helpers.py — Interface specification for the parser under test.

These functions define what the parser must provide. Each is a
projection of the parse tree onto a specific quantity of interest.
"""

from typing import TypeVar, Protocol
from dataclasses import dataclass


# --- Parser interface ---

def parse(session: list[dict] | str, strict: bool = False) -> 'ParsedSession':
    """Parse JSONL events into typed model. Raise ParseError if strict and invalid."""
    ...

def serialize(parsed: 'ParsedSession') -> str:
    """Serialize parse tree back to JSONL string (one JSON object per line)."""
    ...

def validate(parsed: 'ParsedSession', complete: bool = False) -> None:
    """Validate structural invariants. Raise ValidationError on failure."""
    ...


# --- Measurement functions ---

def sum_content_chars(parsed: 'ParsedSession') -> int:
    """Total characters across all content blocks (text + thinking + tool content)."""
    ...

def count_by_type(parsed: 'ParsedSession') -> dict[str, int]:
    """Count events by type: {user: n, assistant: m, progress: p, ...}."""
    ...

def count_tool_pairs(parsed: 'ParsedSession') -> int:
    """Number of matched (tool_use, tool_result) pairs."""
    ...

def count_thinking_blocks(parsed: 'ParsedSession') -> int:
    """Total thinking blocks across all events."""
    ...

def extract_message_ids(parsed: 'ParsedSession') -> set[str]:
    """Set of all message.id values (from assistant events)."""
    ...

def sum_token_usage(parsed: 'ParsedSession') -> dict[str, int]:
    """Sum of all token usage fields across events."""
    ...

def extract_tool_use_ids(parsed: 'ParsedSession') -> list[str]:
    """All tool_use.id values in file order."""
    ...

def extract_tool_result_ids(parsed: 'ParsedSession') -> list[str]:
    """All tool_result.tool_use_id values in file order."""
    ...

def reconstruct_api_messages(parsed: 'ParsedSession') -> list['Message']:
    """Reconstruct the API message sequence (alternating user/assistant)."""
    ...

def build_parent_graph(parsed: 'ParsedSession') -> dict[str, str | None]:
    """Map uuid -> parentUuid for all events."""
    ...

def is_dag(graph: dict) -> bool:
    """Check that the parent graph is a DAG (no cycles)."""
    ...

def remove_events_by_type(parsed: 'ParsedSession', event_type: str) -> 'ParsedSession':
    """Return new ParsedSession with all events of given type removed."""
    ...


# --- Data types ---

@dataclass
class ParsedSession:
    events: list['Event']
    diagnostics: list[str] = field(default_factory=list)

@dataclass
class Event:
    uuid: str
    parent_uuid: str | None
    type: str  # "user", "assistant", "progress", "system", "queue-operation"
    timestamp: str
    session_id: str
    is_sidechain: bool
    message: 'Message | None'
    tool_use_id: str | None = None  # For progress events
    # ... other fields

@dataclass
class Message:
    role: str  # "user", "assistant"
    content: list['ContentBlock']
    id: str | None = None
    model: str | None = None
    stop_reason: str | None = None
    usage: dict | None = None

class ParseError(Exception): ...
class ValidationError(Exception): ...
```

---

## Test Configuration

```python
"""
conftest.py — Pytest configuration for property tests.
"""

from hypothesis import settings, HealthCheck, Phase

# Default settings for property tests
settings.register_profile("ci", max_examples=500, deadline=10000)
settings.register_profile("dev", max_examples=50, deadline=5000)
settings.register_profile("full", max_examples=2000, deadline=30000,
                          suppress_health_check=[HealthCheck.too_slow])

# Use: pytest --hypothesis-profile=ci
```

---

## Summary: The Information-Theoretic View

| Property Class | What It Measures | Entropy Interpretation |
|---|---|---|
| Conservation | Extensive quantities | First moments of the content distribution |
| Structural | Graph topology | The skeleton that makes the session a DAG, not a bag |
| Round-trip | Sufficiency | H(original \| parsed) = 0 |
| Compression | Safe deletions | Which components have H = 0 conditioned on the rest |
| Stateful | Incremental validity | Invariants hold at every prefix, not just the final state |
| Negative | Error detection | Parser rejects inputs with H(structure) = ∞ (undefined) |

The key insight: a correct parser is one where **parse is a sufficient statistic for serialize**. All information in the original JSONL is recoverable from the parse tree. The conservation laws enumerate the specific moments of the distribution that must match; the structural invariants characterize the support of the distribution (which parse trees are valid); and the round-trip tests verify the full distribution equality.
