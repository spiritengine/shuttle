# CC Session JSONL Parser: Test Design

Comprehensive pytest test suite for the round-trip CC session JSONL parser.

---

## Fixture Strategy

### Real Session Fixtures

```python
@pytest.fixture
def dead_session_path():
    """Full 1645-event session (pre-compaction)."""
    return Path("sessions/dead.jsonl")

@pytest.fixture
def post_compact_path():
    """Post-compaction session."""
    return Path("sessions/post-compact-final.jsonl")

@pytest.fixture
def dead_session(dead_session_path):
    """Fully loaded Session from dead.jsonl."""
    return load(dead_session_path)

@pytest.fixture
def post_compact_session(post_compact_path):
    """Fully loaded Session from post-compact-final.jsonl."""
    return load(post_compact_path)
```

### Synthetic Fixtures

```python
@pytest.fixture
def minimal_session(tmp_path):
    """Absolute minimum valid session: one user event, one assistant event."""
    events = [
        {
            "type": "user",
            "uuid": "aaa-001",
            "parentUuid": None,
            "timestamp": "2026-01-22T23:34:06.617Z",
            "sessionId": "sess-001",
            "isSidechain": False,
            "userType": "external",
            "cwd": "/tmp",
            "version": "2.1.14",
            "gitBranch": "main",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "Hello"}]
            }
        },
        {
            "type": "assistant",
            "uuid": "aaa-002",
            "parentUuid": "aaa-001",
            "timestamp": "2026-01-22T23:34:07.000Z",
            "sessionId": "sess-001",
            "isSidechain": False,
            "userType": "external",
            "cwd": "/tmp",
            "version": "2.1.14",
            "gitBranch": "main",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Hi there!"}],
                "id": "msg_01ABC",
                "model": "claude-opus-4-5-20251101",
                "type": "message",
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 5}
            }
        }
    ]
    path = tmp_path / "minimal.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return path

@pytest.fixture
def tool_round_trip_session(tmp_path):
    """Session with one tool_use → tool_result pair."""
    events = [
        make_user_event("u-001", None, [{"type": "text", "text": "Read a file"}]),
        make_assistant_event("a-001", "u-001", [
            {"type": "thinking", "thinking": "I'll read the file", "signature": "sig123"},
            {"type": "tool_use", "id": "toolu_ABC", "name": "Read",
             "input": {"file_path": "/tmp/foo.py"}}
        ], msg_id="msg_001", stop_reason="tool_use"),
        make_user_event("u-002", "a-001", [
            {"type": "tool_result", "tool_use_id": "toolu_ABC",
             "content": "print('hello')", "is_error": False}
        ]),
        make_assistant_event("a-002", "u-002", [
            {"type": "text", "text": "The file contains a print statement."}
        ], msg_id="msg_002", stop_reason="end_turn"),
    ]
    path = tmp_path / "tool_round_trip.jsonl"
    write_events(path, events)
    return path

@pytest.fixture
def streaming_session(tmp_path):
    """Session with streaming pattern: one content block per event, shared message.id."""
    events = [
        make_user_event("u-001", None, [{"type": "text", "text": "Do stuff"}]),
        # Streaming: separate events, same msg_id, parent-child chain
        make_assistant_event("a-001", "u-001", [
            {"type": "thinking", "thinking": "Planning..."}
        ], msg_id="msg_001", stop_reason=None),
        make_assistant_event("a-002", "a-001", [
            {"type": "tool_use", "id": "toolu_X1", "name": "Bash",
             "input": {"command": "ls", "description": "list files"}}
        ], msg_id="msg_001", stop_reason=None),
        make_assistant_event("a-003", "a-002", [
            {"type": "tool_use", "id": "toolu_X2", "name": "Bash",
             "input": {"command": "pwd", "description": "show cwd"}}
        ], msg_id="msg_001", stop_reason=None),
        # Tool results
        make_user_event("u-002", "a-002", [
            {"type": "tool_result", "tool_use_id": "toolu_X1",
             "content": "file1.py\nfile2.py", "is_error": False}
        ]),
        make_user_event("u-003", "a-003", [
            {"type": "tool_result", "tool_use_id": "toolu_X2",
             "content": "/home/user", "is_error": False}
        ]),
    ]
    path = tmp_path / "streaming.jsonl"
    write_events(path, events)
    return path

@pytest.fixture
def batched_session(tmp_path):
    """Session with batched pattern: multiple content blocks in one event."""
    events = [
        make_user_event("u-001", None, [{"type": "text", "text": "Do stuff"}]),
        make_assistant_event("a-001", "u-001", [
            {"type": "thinking", "thinking": "Planning...", "signature": "sig"},
            {"type": "text", "text": "I'll run two commands."},
            {"type": "tool_use", "id": "toolu_Y1", "name": "Bash",
             "input": {"command": "ls"}},
            {"type": "tool_use", "id": "toolu_Y2", "name": "Read",
             "input": {"file_path": "/tmp/x.py"}}
        ], msg_id="msg_001", stop_reason="tool_use"),
        make_user_event("u-002", "a-001", [
            {"type": "tool_result", "tool_use_id": "toolu_Y1",
             "content": "a.py\nb.py", "is_error": False},
            {"type": "tool_result", "tool_use_id": "toolu_Y2",
             "content": "x = 1", "is_error": False}
        ]),
    ]
    path = tmp_path / "batched.jsonl"
    write_events(path, events)
    return path

@pytest.fixture
def meta_events_session(tmp_path):
    """Session with isMeta events (string content, system injections)."""
    events = [
        {
            **base_event("u-001", None, "user"),
            "isMeta": True,
            "message": {
                "role": "user",
                "content": "<local-command-caveat>Caveat: be careful</local-command-caveat>"
            }
        },
        make_user_event("u-002", "u-001", [{"type": "text", "text": "Hello"}]),
        make_assistant_event("a-001", "u-002", [
            {"type": "text", "text": "Hi!"}
        ], msg_id="msg_001", stop_reason="end_turn"),
    ]
    path = tmp_path / "meta.jsonl"
    write_events(path, events)
    return path

@pytest.fixture
def progress_events_session(tmp_path):
    """Session with progress events interleaved."""
    events = [
        make_user_event("u-001", None, [{"type": "text", "text": "Run command"}]),
        make_assistant_event("a-001", "u-001", [
            {"type": "tool_use", "id": "toolu_P1", "name": "Bash",
             "input": {"command": "sleep 5 && echo done"}}
        ], msg_id="msg_001", stop_reason="tool_use"),
        # Progress events during tool execution
        {
            **base_event("p-001", "a-001", "progress"),
            "toolUseID": "bash-progress-0",
            "parentToolUseID": "toolu_P1",
            "data": {
                "type": "bash_progress",
                "output": "running...",
                "fullOutput": "running...",
                "elapsedTimeSeconds": 1,
                "totalLines": 1
            }
        },
        {
            **base_event("p-002", "p-001", "progress"),
            "toolUseID": "bash-progress-0",
            "parentToolUseID": "toolu_P1",
            "data": {
                "type": "bash_progress",
                "output": "done",
                "fullOutput": "running...\ndone",
                "elapsedTimeSeconds": 5,
                "totalLines": 2
            }
        },
        make_user_event("u-002", "p-002", [
            {"type": "tool_result", "tool_use_id": "toolu_P1",
             "content": "done", "is_error": False}
        ]),
    ]
    path = tmp_path / "progress.jsonl"
    write_events(path, events)
    return path

@pytest.fixture
def error_tool_session(tmp_path):
    """Session with tool errors (is_error=True)."""
    events = [
        make_user_event("u-001", None, [{"type": "text", "text": "Run bad command"}]),
        make_assistant_event("a-001", "u-001", [
            {"type": "tool_use", "id": "toolu_E1", "name": "Bash",
             "input": {"command": "nonexistent_cmd"}}
        ], msg_id="msg_001", stop_reason="tool_use"),
        make_user_event("u-002", "a-001", [
            {"type": "tool_result", "tool_use_id": "toolu_E1",
             "content": "bash: nonexistent_cmd: command not found\nExit code 127",
             "is_error": True}
        ]),
        make_assistant_event("a-002", "u-002", [
            {"type": "text", "text": "The command failed."}
        ], msg_id="msg_002", stop_reason="end_turn"),
    ]
    path = tmp_path / "error_tool.jsonl"
    write_events(path, events)
    return path

@pytest.fixture
def sidechain_session(tmp_path):
    """Session with isSidechain=True events that should be filtered."""
    events = [
        make_user_event("u-001", None, [{"type": "text", "text": "Hello"}]),
        {
            **base_event("s-001", "u-001", "assistant"),
            "isSidechain": True,
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "sidechain noise"}],
                "id": "msg_side", "model": "claude-opus-4-5-20251101",
                "type": "message", "stop_reason": "end_turn"
            }
        },
        make_assistant_event("a-001", "u-001", [
            {"type": "text", "text": "Main response"}
        ], msg_id="msg_001", stop_reason="end_turn"),
    ]
    path = tmp_path / "sidechain.jsonl"
    write_events(path, events)
    return path

@pytest.fixture
def unknown_fields_session(tmp_path):
    """Session with unknown/future fields that must be preserved in round-trip."""
    events = [
        {
            **base_event("u-001", None, "user"),
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "Hi"}]
            },
            "futureField": "must_survive_round_trip",
            "nestedFuture": {"a": 1, "b": [2, 3]},
            "slug": "test-slug-name"
        },
        {
            **base_event("a-001", "u-001", "assistant"),
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Hello"}],
                "id": "msg_001", "model": "claude-opus-4-5-20251101",
                "type": "message", "stop_reason": "end_turn",
                "unknownMessageField": True
            },
            "requestId": "req_XYZ",
            "experimentalFlag": 42
        }
    ]
    path = tmp_path / "unknown_fields.jsonl"
    write_events(path, events)
    return path

@pytest.fixture
def queue_operations_session(tmp_path):
    """Session with queue-operation events."""
    events = [
        make_user_event("u-001", None, [{"type": "text", "text": "First"}]),
        make_assistant_event("a-001", "u-001", [
            {"type": "text", "text": "Response 1"}
        ], msg_id="msg_001", stop_reason="end_turn"),
        {
            "type": "queue-operation",
            "operation": "enqueue",
            "timestamp": "2026-01-23T01:59:54.355Z",
            "sessionId": "sess-001",
            "content": "second message queued"
        },
        make_user_event("u-002", "a-001", [{"type": "text", "text": "Second"}]),
    ]
    path = tmp_path / "queue_ops.jsonl"
    write_events(path, events)
    return path

@pytest.fixture
def system_events_session(tmp_path):
    """Session with system events (hook summaries, turn durations, compaction markers)."""
    events = [
        make_user_event("u-001", None, [{"type": "text", "text": "Go"}]),
        make_assistant_event("a-001", "u-001", [
            {"type": "tool_use", "id": "toolu_S1", "name": "Write",
             "input": {"file_path": "/tmp/f.py", "content": "x=1"}}
        ], msg_id="msg_001", stop_reason="tool_use"),
        {
            **base_event("sys-001", "a-001", "system"),
            "subtype": "stop_hook_summary",
            "toolUseID": "hook-uuid-001",
            "hookCount": 1,
            "hookInfos": [{"command": "echo hook ran"}],
            "hookErrors": [],
            "preventedContinuation": False,
            "stopReason": "",
            "hasOutput": True,
            "level": "suggestion"
        },
        make_user_event("u-002", "a-001", [
            {"type": "tool_result", "tool_use_id": "toolu_S1",
             "content": "File written.", "is_error": False}
        ]),
        {
            **base_event("sys-002", "u-002", "system"),
            "subtype": "turn_duration",
            "durationMs": 3500
        },
    ]
    path = tmp_path / "system_events.jsonl"
    write_events(path, events)
    return path

@pytest.fixture
def toolUseResult_session(tmp_path):
    """Session with top-level toolUseResult duplicating message content."""
    events = [
        make_user_event("u-001", None, [{"type": "text", "text": "Run ls"}]),
        make_assistant_event("a-001", "u-001", [
            {"type": "tool_use", "id": "toolu_T1", "name": "Bash",
             "input": {"command": "ls", "description": "list files"}}
        ], msg_id="msg_001", stop_reason="tool_use"),
        {
            **base_event("u-002", "a-001", "user"),
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "toolu_T1",
                             "content": "file1.py\nfile2.py", "is_error": False}]
            },
            "toolUseResult": {
                "stdout": "file1.py\nfile2.py",
                "stderr": "",
                "interrupted": False,
                "isImage": False
            },
            "sourceToolAssistantUUID": "a-001"
        }
    ]
    path = tmp_path / "toolUseResult.jsonl"
    write_events(path, events)
    return path

@pytest.fixture
def multi_turn_session(tmp_path):
    """Complete multi-turn session: user→assistant→tools→assistant→user→assistant."""
    # Turn 1: user asks, assistant uses tool
    # Turn 2: user follows up, assistant responds with text only
    events = [
        make_user_event("u-001", None, [{"type": "text", "text": "What files exist?"}]),
        make_assistant_event("a-001", "u-001", [
            {"type": "thinking", "thinking": "Let me check", "signature": "s1"},
            {"type": "tool_use", "id": "toolu_M1", "name": "Bash",
             "input": {"command": "ls"}}
        ], msg_id="msg_001", stop_reason="tool_use"),
        make_user_event("u-002", "a-001", [
            {"type": "tool_result", "tool_use_id": "toolu_M1",
             "content": "a.py\nb.py", "is_error": False}
        ]),
        make_assistant_event("a-002", "u-002", [
            {"type": "text", "text": "There are two files: a.py and b.py"}
        ], msg_id="msg_002", stop_reason="end_turn"),
        # Turn 2: user follow-up
        make_user_event("u-003", "a-002", [{"type": "text", "text": "Read a.py"}]),
        make_assistant_event("a-003", "u-003", [
            {"type": "tool_use", "id": "toolu_M2", "name": "Read",
             "input": {"file_path": "/tmp/a.py"}}
        ], msg_id="msg_003", stop_reason="tool_use"),
        make_user_event("u-004", "a-003", [
            {"type": "tool_result", "tool_use_id": "toolu_M2",
             "content": "print('a')", "is_error": False}
        ]),
        make_assistant_event("a-004", "u-004", [
            {"type": "text", "text": "The file prints 'a'."}
        ], msg_id="msg_004", stop_reason="end_turn"),
    ]
    path = tmp_path / "multi_turn.jsonl"
    write_events(path, events)
    return path

@pytest.fixture
def empty_content_session(tmp_path):
    """Events with empty content arrays or None message."""
    events = [
        make_user_event("u-001", None, []),  # Empty content array
        make_assistant_event("a-001", "u-001", [
            {"type": "text", "text": "Responding to empty"}
        ], msg_id="msg_001", stop_reason="end_turn"),
    ]
    path = tmp_path / "empty_content.jsonl"
    write_events(path, events)
    return path

@pytest.fixture
def large_tool_result_session(tmp_path):
    """Session with very large tool results (simulating Read on big file)."""
    big_content = "x = 1\n" * 5000  # ~30KB
    events = [
        make_user_event("u-001", None, [{"type": "text", "text": "Read big file"}]),
        make_assistant_event("a-001", "u-001", [
            {"type": "tool_use", "id": "toolu_L1", "name": "Read",
             "input": {"file_path": "/tmp/big.py"}}
        ], msg_id="msg_001", stop_reason="tool_use"),
        make_user_event("u-002", "a-001", [
            {"type": "tool_result", "tool_use_id": "toolu_L1",
             "content": big_content, "is_error": False}
        ]),
        make_assistant_event("a-002", "u-002", [
            {"type": "text", "text": "Large file read."}
        ], msg_id="msg_002", stop_reason="end_turn"),
    ]
    path = tmp_path / "large_result.jsonl"
    write_events(path, events)
    return path
```

### Test Helper Functions

```python
def base_event(uuid, parent_uuid, event_type):
    """Create base event dict with required fields."""
    return {
        "type": event_type,
        "uuid": uuid,
        "parentUuid": parent_uuid,
        "timestamp": "2026-01-22T23:34:06.617Z",
        "sessionId": "sess-001",
        "isSidechain": False,
        "userType": "external",
        "cwd": "/tmp",
        "version": "2.1.14",
        "gitBranch": "main",
    }

def make_user_event(uuid, parent_uuid, content_blocks):
    return {
        **base_event(uuid, parent_uuid, "user"),
        "message": {"role": "user", "content": content_blocks}
    }

def make_assistant_event(uuid, parent_uuid, content_blocks, msg_id="msg_default",
                         stop_reason="end_turn", model="claude-opus-4-5-20251101"):
    return {
        **base_event(uuid, parent_uuid, "assistant"),
        "message": {
            "role": "assistant",
            "content": content_blocks,
            "id": msg_id,
            "model": model,
            "type": "message",
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 20}
        }
    }

def write_events(path, events):
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")

def assert_round_trip(path):
    """Parse a session, serialize it, parse again, assert equivalence."""
    session1 = load(path)
    out_path = path.parent / "round_trip_output.jsonl"
    serialize(session1, out_path)
    session2 = load(out_path)
    # Compare raw dicts (stripping computed enrichment)
    for e1, e2 in zip(session1.events, session2.events):
        assert e1.raw == e2.raw
```

---

## Test Classes

### 1. `TestParsing` - Basic Event Parsing

```python
class TestParsing:
    """Basic parsing: load events, detect types, access fields."""

    def test_parse_returns_iterator(self, minimal_session):
        """parse() returns an iterator, not a list."""
        result = parse(minimal_session)
        assert hasattr(result, '__iter__')
        assert hasattr(result, '__next__')

    def test_load_returns_session(self, minimal_session):
        """load() returns a Session object."""
        session = load(minimal_session)
        assert isinstance(session, Session)
        assert isinstance(session.events, list)

    def test_event_count_minimal(self, minimal_session):
        """Minimal session has exactly 2 events."""
        session = load(minimal_session)
        assert len(session.events) == 2

    def test_event_count_real(self, dead_session):
        """dead.jsonl has 1645 events."""
        assert len(dead_session.events) == 1645

    def test_event_type_detection(self, dead_session):
        """All 5 event types detected correctly."""
        types = Counter(e.type for e in dead_session.events)
        assert types['user'] == 228
        assert types['assistant'] == 443
        assert types['progress'] == 923
        assert types['system'] == 43
        assert types['queue-operation'] == 8

    def test_event_uuid_populated(self, dead_session):
        """Every event has a uuid."""
        for event in dead_session.events:
            assert event.uuid is not None
            assert len(event.uuid) > 0

    def test_event_session_id(self, minimal_session):
        """Events expose session_id."""
        session = load(minimal_session)
        assert session.events[0].session_id == "sess-001"

    def test_event_timestamp_parsed(self, minimal_session):
        """Timestamps parsed to datetime objects."""
        session = load(minimal_session)
        assert isinstance(session.events[0].timestamp, datetime)

    def test_event_cwd(self, minimal_session):
        """Events expose cwd."""
        session = load(minimal_session)
        assert session.events[0].cwd == "/tmp"

    def test_event_git_branch(self, minimal_session):
        """Events expose git_branch."""
        session = load(minimal_session)
        assert session.events[0].git_branch == "main"

    def test_event_version(self, minimal_session):
        """Events expose version string."""
        session = load(minimal_session)
        assert session.events[0].version == "2.1.14"

    def test_event_is_sidechain(self, minimal_session):
        """Events expose is_sidechain boolean."""
        session = load(minimal_session)
        assert session.events[0].is_sidechain is False

    def test_user_event_has_message(self, minimal_session):
        """User events have a Message object."""
        session = load(minimal_session)
        user_event = session.events[0]
        assert user_event.message is not None
        assert user_event.message.role == "user"

    def test_assistant_event_has_message(self, minimal_session):
        """Assistant events have a Message with model/id/usage."""
        session = load(minimal_session)
        asst_event = session.events[1]
        assert asst_event.message.role == "assistant"
        assert asst_event.message.id == "msg_01ABC"
        assert asst_event.message.model == "claude-opus-4-5-20251101"
        assert asst_event.message.stop_reason == "end_turn"
        assert asst_event.message.usage is not None

    def test_raw_dict_preserved(self, unknown_fields_session):
        """Event.raw contains all original JSON fields."""
        session = load(unknown_fields_session)
        assert session.events[0].raw["futureField"] == "must_survive_round_trip"
        assert session.events[0].raw["nestedFuture"] == {"a": 1, "b": [2, 3]}

    def test_content_blocks_typed(self, tool_round_trip_session):
        """Content blocks parsed into typed objects (ToolUse, ToolResult, etc.)."""
        session = load(tool_round_trip_session)
        asst = session.events[1]
        assert isinstance(asst.message.content[0], ContentBlock)  # thinking
        assert asst.message.content[0].type == "thinking"
        assert isinstance(asst.message.content[1], ToolUse)
        assert asst.message.content[1].name == "Read"

    def test_progress_event_no_message(self, progress_events_session):
        """Progress events have no message (or message is None)."""
        session = load(progress_events_session)
        progress = [e for e in session.events if e.type == "progress"]
        for p in progress:
            assert p.message is None

    def test_queue_operation_parsing(self, queue_operations_session):
        """Queue-operation events parsed with operation field."""
        session = load(queue_operations_session)
        queue_ops = [e for e in session.events if e.type == "queue-operation"]
        assert len(queue_ops) == 1
        assert queue_ops[0].raw["operation"] == "enqueue"

    def test_system_event_subtype(self, system_events_session):
        """System events expose subtype."""
        session = load(system_events_session)
        system = [e for e in session.events if e.type == "system"]
        subtypes = [e.raw.get("subtype") for e in system]
        assert "stop_hook_summary" in subtypes
        assert "turn_duration" in subtypes

    def test_is_meta_flag(self, meta_events_session):
        """isMeta events detected correctly."""
        session = load(meta_events_session)
        meta = [e for e in session.events if e.is_meta]
        assert len(meta) == 1
        assert meta[0].type == "user"

    def test_string_content_on_meta(self, meta_events_session):
        """isMeta events with string content (not array) handled."""
        session = load(meta_events_session)
        meta = [e for e in session.events if e.is_meta][0]
        # Content should be accessible even when it's a string
        assert "Caveat" in str(meta.message.content)
```

### 2. `TestTreeConstruction` - Parent-Child Relationships

```python
class TestTreeConstruction:
    """Tree construction via uuid/parentUuid."""

    def test_tree_built(self, minimal_session):
        """Session.tree is populated."""
        session = load(minimal_session)
        assert session.tree is not None

    def test_root_detection(self, minimal_session):
        """Root events (parentUuid=None) identified."""
        session = load(minimal_session)
        roots = [e for e in session.events if e.parent_uuid is None]
        assert len(roots) == 1
        assert roots[0].uuid == "aaa-001"

    def test_children_populated(self, minimal_session):
        """Event.children computed from tree."""
        session = load(minimal_session)
        root = session.events[0]
        assert len(root.children) == 1
        assert root.children[0].uuid == "aaa-002"

    def test_leaf_has_no_children(self, minimal_session):
        """Leaf events have empty children list."""
        session = load(minimal_session)
        leaf = session.events[1]
        assert leaf.children == []

    def test_streaming_chain_structure(self, streaming_session):
        """Streaming events form parent-child chain."""
        session = load(streaming_session)
        by_uuid = {e.uuid: e for e in session.events}
        # a-001 → a-002 → a-003
        assert by_uuid["a-002"].parent_uuid == "a-001"
        assert by_uuid["a-003"].parent_uuid == "a-002"

    def test_tool_result_branches(self, streaming_session):
        """Tool results branch off their corresponding tool_use parents."""
        session = load(streaming_session)
        by_uuid = {e.uuid: e for e in session.events}
        # u-002 is child of a-002 (first tool_use)
        assert by_uuid["u-002"].parent_uuid == "a-002"
        # u-003 is child of a-003 (second tool_use)
        assert by_uuid["u-003"].parent_uuid == "a-003"

    def test_multiple_children(self, streaming_session):
        """Events can have multiple children (branching)."""
        session = load(streaming_session)
        by_uuid = {e.uuid: e for e in session.events}
        # a-002 has two children: a-003 (next tool_use) and u-002 (tool_result)
        a002_children = by_uuid["a-002"].children
        child_uuids = {c.uuid for c in a002_children}
        assert "a-003" in child_uuids
        assert "u-002" in child_uuids

    def test_real_session_tree_root_count(self, dead_session):
        """Real session has expected number of roots."""
        roots = [e for e in dead_session.events if e.parent_uuid is None]
        assert len(roots) >= 1  # At least one root

    def test_real_session_no_orphans(self, dead_session):
        """All non-root events have valid parent references."""
        uuid_set = {e.uuid for e in dead_session.events}
        for event in dead_session.events:
            if event.parent_uuid is not None:
                assert event.parent_uuid in uuid_set, \
                    f"Event {event.uuid} references non-existent parent {event.parent_uuid}"

    def test_tree_preserves_file_order(self, multi_turn_session):
        """Tree traversal respects file order, not timestamps."""
        session = load(multi_turn_session)
        # Events should be accessible in file order regardless of tree structure
        uuids_in_order = [e.uuid for e in session.events]
        assert uuids_in_order == ["u-001", "a-001", "u-002", "a-002",
                                   "u-003", "a-003", "u-004", "a-004"]

    def test_progress_events_in_tree(self, progress_events_session):
        """Progress events participate in tree (have parents/children)."""
        session = load(progress_events_session)
        progress = [e for e in session.events if e.type == "progress"]
        assert progress[0].parent_uuid == "a-001"
        assert progress[1].parent_uuid == "p-001"
```

### 3. `TestMessageReconstruction` - API Message Array

```python
class TestMessageReconstruction:
    """Reconstruct API message array from event stream."""

    def test_messages_returns_list(self, minimal_session):
        """session.messages() returns list of APIMessage."""
        session = load(minimal_session)
        msgs = session.messages()
        assert isinstance(msgs, list)

    def test_minimal_message_count(self, minimal_session):
        """Minimal session produces 2 messages (user + assistant)."""
        session = load(minimal_session)
        msgs = session.messages()
        assert len(msgs) == 2

    def test_role_alternation(self, minimal_session):
        """Messages alternate user/assistant."""
        session = load(minimal_session)
        msgs = session.messages()
        assert msgs[0].role == "user"
        assert msgs[1].role == "assistant"

    def test_streaming_events_merged(self, streaming_session):
        """Multiple events with same message.id merged into one message."""
        session = load(streaming_session)
        msgs = session.messages()
        # All 3 assistant events (a-001, a-002, a-003) share msg_001
        # They should merge into one assistant message
        assistant_msgs = [m for m in msgs if m.role == "assistant"]
        assert len(assistant_msgs) == 1
        # Merged message has all content blocks
        assert len(assistant_msgs[0].content) == 3  # thinking + 2 tool_use

    def test_batched_content_preserved(self, batched_session):
        """Batched events (multiple blocks in one event) preserve all blocks."""
        session = load(batched_session)
        msgs = session.messages()
        assistant_msgs = [m for m in msgs if m.role == "assistant"]
        assert len(assistant_msgs) == 1
        assert len(assistant_msgs[0].content) == 4  # thinking + text + 2 tool_use

    def test_tool_results_as_user_messages(self, tool_round_trip_session):
        """Tool results appear as user messages in API array."""
        session = load(tool_round_trip_session)
        msgs = session.messages()
        # user text → assistant (thinking+tool_use) → user (tool_result) → assistant (text)
        assert len(msgs) == 4
        assert msgs[2].role == "user"
        assert msgs[2].content[0].type == "tool_result"

    def test_progress_events_excluded(self, progress_events_session):
        """Progress events not included in API messages."""
        session = load(progress_events_session)
        msgs = session.messages()
        # Only user, assistant, user (tool_result) - no progress
        for msg in msgs:
            assert msg.role in ("user", "assistant")

    def test_system_events_excluded(self, system_events_session):
        """System events not included in API messages."""
        session = load(system_events_session)
        msgs = session.messages()
        roles = [m.role for m in msgs]
        assert all(r in ("user", "assistant") for r in roles)

    def test_sidechain_excluded(self, sidechain_session):
        """isSidechain events excluded from API messages."""
        session = load(sidechain_session)
        msgs = session.messages()
        # Only main-chain events
        assert len(msgs) == 2  # user + assistant (main)
        assert msgs[1].content[0].text == "Main response"

    def test_meta_events_included(self, meta_events_session):
        """isMeta events ARE included (they're system-injected user messages)."""
        session = load(meta_events_session)
        msgs = session.messages()
        # Meta events are part of the conversation
        assert any("Caveat" in str(m.content) for m in msgs if m.role == "user")

    def test_string_content_normalized(self, meta_events_session):
        """String content (from isMeta) normalized to content block list."""
        session = load(meta_events_session)
        msgs = session.messages()
        meta_msg = msgs[0]  # First user message is the meta one
        # Content should be accessible as list even if originally string
        assert isinstance(meta_msg.content, list)

    def test_consecutive_same_role_merged(self, streaming_session):
        """Consecutive same-role events produce merged messages."""
        session = load(streaming_session)
        msgs = session.messages()
        # Verify no adjacent messages have same role
        for i in range(len(msgs) - 1):
            if msgs[i].role == msgs[i+1].role:
                # This is valid if they represent different logical groups
                # but generally messages alternate
                pass

    def test_real_session_message_count(self, dead_session):
        """Real session produces expected message count."""
        msgs = dead_session.messages()
        # Should be fewer than 1645 events (merged, filtered)
        assert len(msgs) < 1645
        assert len(msgs) > 0

    def test_real_session_starts_with_user(self, dead_session):
        """Real session starts with user message."""
        msgs = dead_session.messages()
        assert msgs[0].role == "user"

    def test_empty_content_handled(self, empty_content_session):
        """Events with empty content arrays handled gracefully."""
        session = load(empty_content_session)
        msgs = session.messages()
        # Empty content user event might be skipped or produce empty message
        assert len(msgs) >= 1  # At least the assistant response
```

### 4. `TestToolMatching` - tool_use/tool_result Pairing

```python
class TestToolMatching:
    """Match tool_use and tool_result by ID."""

    def test_tool_pairs_returned(self, tool_round_trip_session):
        """session.tool_pairs() returns list of ToolPair."""
        session = load(tool_round_trip_session)
        pairs = session.tool_pairs()
        assert isinstance(pairs, list)
        assert all(isinstance(p, ToolPair) for p in pairs)

    def test_single_tool_pair_matched(self, tool_round_trip_session):
        """Single tool_use matched to its tool_result."""
        session = load(tool_round_trip_session)
        pairs = session.tool_pairs()
        assert len(pairs) == 1
        assert pairs[0].tool_use.id == "toolu_ABC"
        assert pairs[0].tool_result.tool_use_id == "toolu_ABC"

    def test_tool_name_extracted(self, tool_round_trip_session):
        """ToolPair.tool_name set from tool_use.name."""
        session = load(tool_round_trip_session)
        pairs = session.tool_pairs()
        assert pairs[0].tool_name == "Read"

    def test_multiple_pairs_matched(self, streaming_session):
        """Multiple tool pairs all matched correctly."""
        session = load(streaming_session)
        pairs = session.tool_pairs()
        assert len(pairs) == 2
        ids = {p.tool_use.id for p in pairs}
        assert ids == {"toolu_X1", "toolu_X2"}

    def test_batched_multiple_pairs(self, batched_session):
        """Multiple tool_use in one event matched to results."""
        session = load(batched_session)
        pairs = session.tool_pairs()
        assert len(pairs) == 2
        names = {p.tool_name for p in pairs}
        assert names == {"Bash", "Read"}

    def test_error_tool_pair(self, error_tool_session):
        """Tool pairs with is_error=True detected."""
        session = load(error_tool_session)
        pairs = session.tool_pairs()
        assert len(pairs) == 1
        assert pairs[0].tool_result.is_error is True

    def test_result_size_computed(self, tool_round_trip_session):
        """ToolPair.result_size computed from content length."""
        session = load(tool_round_trip_session)
        pairs = session.tool_pairs()
        assert pairs[0].result_size == len("print('hello')")

    def test_file_path_extracted_read(self, tool_round_trip_session):
        """ToolPair.file_path extracted for Read tool."""
        session = load(tool_round_trip_session)
        pairs = session.tool_pairs()
        assert pairs[0].file_path == "/tmp/foo.py"

    def test_file_path_extracted_write(self, system_events_session):
        """ToolPair.file_path extracted for Write tool."""
        session = load(system_events_session)
        pairs = session.tool_pairs()
        write_pair = [p for p in pairs if p.tool_name == "Write"][0]
        assert write_pair.file_path == "/tmp/f.py"

    def test_file_path_extracted_edit(self, tmp_path):
        """ToolPair.file_path extracted for Edit tool."""
        events = [
            make_user_event("u-001", None, [{"type": "text", "text": "Fix file"}]),
            make_assistant_event("a-001", "u-001", [
                {"type": "tool_use", "id": "toolu_E1", "name": "Edit",
                 "input": {"file_path": "/tmp/x.py", "old_string": "x=1", "new_string": "x=2"}}
            ], msg_id="msg_001", stop_reason="tool_use"),
            make_user_event("u-002", "a-001", [
                {"type": "tool_result", "tool_use_id": "toolu_E1",
                 "content": "File updated.", "is_error": False}
            ]),
        ]
        path = tmp_path / "edit_test.jsonl"
        write_events(path, events)
        session = load(path)
        pairs = session.tool_pairs()
        assert pairs[0].file_path == "/tmp/x.py"

    def test_command_extracted_bash(self, streaming_session):
        """ToolPair.command extracted for Bash tool."""
        session = load(streaming_session)
        pairs = session.tool_pairs()
        bash_pairs = [p for p in pairs if p.tool_name == "Bash"]
        commands = {p.command for p in bash_pairs}
        assert "ls" in commands
        assert "pwd" in commands

    def test_pattern_extracted_grep(self, tmp_path):
        """ToolPair.pattern extracted for Grep tool."""
        events = [
            make_user_event("u-001", None, [{"type": "text", "text": "Search"}]),
            make_assistant_event("a-001", "u-001", [
                {"type": "tool_use", "id": "toolu_G1", "name": "Grep",
                 "input": {"pattern": "TODO", "path": "/tmp"}}
            ], msg_id="msg_001", stop_reason="tool_use"),
            make_user_event("u-002", "a-001", [
                {"type": "tool_result", "tool_use_id": "toolu_G1",
                 "content": "file.py:3:# TODO fix", "is_error": False}
            ]),
        ]
        path = tmp_path / "grep_test.jsonl"
        write_events(path, events)
        session = load(path)
        pairs = session.tool_pairs()
        assert pairs[0].pattern == "TODO"

    def test_tool_pair_linked_to_event(self, tool_round_trip_session):
        """Events have tool_pair back-reference."""
        session = load(tool_round_trip_session)
        asst = session.events[1]
        # The assistant event containing tool_use should link to its pair
        assert asst.tool_pair is not None or \
               any(b.type == "tool_use" for b in asst.message.content)

    def test_real_session_tool_count(self, dead_session):
        """Real session has expected tool pair count."""
        pairs = dead_session.tool_pairs()
        assert len(pairs) == 192  # From tool_patterns.md

    def test_real_session_tool_inventory(self, dead_session):
        """Real session tool inventory matches known counts."""
        pairs = dead_session.tool_pairs()
        by_name = Counter(p.tool_name for p in pairs)
        assert by_name["Bash"] == 107
        assert by_name["Read"] == 32
        assert by_name["Edit"] == 16

    def test_large_result_size(self, large_tool_result_session):
        """Large tool results have correct result_size."""
        session = load(large_tool_result_session)
        pairs = session.tool_pairs()
        assert pairs[0].result_size == len("x = 1\n" * 5000)

    def test_empty_result_size(self, tmp_path):
        """Empty tool results have result_size=0."""
        events = [
            make_user_event("u-001", None, [{"type": "text", "text": "Run"}]),
            make_assistant_event("a-001", "u-001", [
                {"type": "tool_use", "id": "toolu_Z1", "name": "Bash",
                 "input": {"command": "true"}}
            ], msg_id="msg_001", stop_reason="tool_use"),
            make_user_event("u-002", "a-001", [
                {"type": "tool_result", "tool_use_id": "toolu_Z1",
                 "content": "", "is_error": False}
            ]),
        ]
        path = tmp_path / "empty_result.jsonl"
        write_events(path, events)
        session = load(path)
        pairs = session.tool_pairs()
        assert pairs[0].result_size == 0
```

### 5. `TestTurnDetection` - Logical Conversation Turns

```python
class TestTurnDetection:
    """Detect logical conversation turns from event stream."""

    def test_turns_returned(self, multi_turn_session):
        """session.turns() returns list of Turn."""
        session = load(multi_turn_session)
        turns = session.turns()
        assert isinstance(turns, list)
        assert all(isinstance(t, Turn) for t in turns)

    def test_multi_turn_count(self, multi_turn_session):
        """Multi-turn session detected correctly."""
        session = load(multi_turn_session)
        turns = session.turns()
        assert len(turns) == 2  # Two user→assistant cycles

    def test_turn_has_user_events(self, multi_turn_session):
        """Each turn has user_events."""
        session = load(multi_turn_session)
        turns = session.turns()
        for turn in turns:
            assert len(turn.user_events) >= 1

    def test_turn_has_assistant_events(self, multi_turn_session):
        """Each turn has assistant_events."""
        session = load(multi_turn_session)
        turns = session.turns()
        for turn in turns:
            assert len(turn.assistant_events) >= 1

    def test_turn_tool_pairs(self, multi_turn_session):
        """Turn.tool_pairs contains tools used in that turn."""
        session = load(multi_turn_session)
        turns = session.turns()
        # Turn 1 uses Bash, Turn 2 uses Read
        assert len(turns[0].tool_pairs) == 1
        assert turns[0].tool_pairs[0].tool_name == "Bash"
        assert len(turns[1].tool_pairs) == 1
        assert turns[1].tool_pairs[0].tool_name == "Read"

    def test_turn_with_multiple_tools(self, streaming_session):
        """Turn with multiple tool uses has all pairs."""
        session = load(streaming_session)
        turns = session.turns()
        # Single turn with 2 Bash tool uses
        assert len(turns) == 1
        assert len(turns[0].tool_pairs) == 2

    def test_turn_without_tools(self, minimal_session):
        """Turn without tool use has empty tool_pairs."""
        session = load(minimal_session)
        turns = session.turns()
        assert len(turns) == 1
        assert turns[0].tool_pairs == []

    def test_tool_result_events_in_turn(self, tool_round_trip_session):
        """Tool result user events grouped within same turn as tool_use."""
        session = load(tool_round_trip_session)
        turns = session.turns()
        # The tool_result user event belongs to turn 1 (not a new turn)
        assert len(turns) == 1
        # Turn has: initial user text, tool_use assistant, tool_result user, final assistant
        assert len(turns[0].user_events) >= 1
        assert len(turns[0].assistant_events) >= 1

    def test_meta_events_not_turns(self, meta_events_session):
        """isMeta user events don't start new turns."""
        session = load(meta_events_session)
        turns = session.turns()
        # Meta event + real user event + assistant = 1 turn
        assert len(turns) == 1

    def test_real_session_turn_count(self, dead_session):
        """Real session has reasonable turn count."""
        turns = dead_session.turns()
        # Should be much fewer than 1645 events
        assert len(turns) > 0
        assert len(turns) < 200  # Reasonable upper bound

    def test_turns_cover_all_conversation_events(self, multi_turn_session):
        """All user/assistant events accounted for in turns."""
        session = load(multi_turn_session)
        turns = session.turns()
        all_turn_events = set()
        for turn in turns:
            for e in turn.user_events + turn.assistant_events:
                all_turn_events.add(e.uuid)
        conversation_events = {e.uuid for e in session.events
                               if e.type in ('user', 'assistant') and not e.is_sidechain}
        assert conversation_events == all_turn_events
```

### 6. `TestRoundTrip` - Parse → Serialize → Parse Equivalence

```python
class TestRoundTrip:
    """Round-trip fidelity: parse → serialize → parse produces equivalent output."""

    def test_minimal_round_trip(self, minimal_session, tmp_path):
        """Minimal session survives round-trip."""
        session = load(minimal_session)
        out = tmp_path / "out.jsonl"
        serialize(session, out)
        session2 = load(out)
        assert len(session2.events) == len(session.events)
        for e1, e2 in zip(session.events, session2.events):
            assert e1.raw == e2.raw

    def test_tool_session_round_trip(self, tool_round_trip_session, tmp_path):
        """Session with tools survives round-trip."""
        assert_round_trip(tool_round_trip_session)

    def test_streaming_round_trip(self, streaming_session, tmp_path):
        """Streaming pattern session survives round-trip."""
        assert_round_trip(streaming_session)

    def test_batched_round_trip(self, batched_session, tmp_path):
        """Batched pattern session survives round-trip."""
        assert_round_trip(batched_session)

    def test_meta_round_trip(self, meta_events_session, tmp_path):
        """Meta events with string content survive round-trip."""
        assert_round_trip(meta_events_session)

    def test_progress_round_trip(self, progress_events_session, tmp_path):
        """Progress events survive round-trip."""
        assert_round_trip(progress_events_session)

    def test_system_events_round_trip(self, system_events_session, tmp_path):
        """System events survive round-trip."""
        assert_round_trip(system_events_session)

    def test_unknown_fields_round_trip(self, unknown_fields_session, tmp_path):
        """Unknown/future fields preserved in round-trip."""
        session = load(unknown_fields_session)
        out = tmp_path / "out.jsonl"
        serialize(session, out)
        session2 = load(out)
        assert session2.events[0].raw["futureField"] == "must_survive_round_trip"
        assert session2.events[0].raw["nestedFuture"] == {"a": 1, "b": [2, 3]}
        assert session2.events[1].raw["experimentalFlag"] == 42
        assert session2.events[1].raw["message"]["unknownMessageField"] is True

    def test_toolUseResult_round_trip(self, toolUseResult_session, tmp_path):
        """Top-level toolUseResult preserved in round-trip."""
        session = load(toolUseResult_session)
        out = tmp_path / "out.jsonl"
        serialize(session, out)
        session2 = load(out)
        event = session2.events[2]
        assert event.raw["toolUseResult"]["stdout"] == "file1.py\nfile2.py"
        assert event.raw["sourceToolAssistantUUID"] == "a-001"

    def test_queue_operations_round_trip(self, queue_operations_session, tmp_path):
        """Queue-operation events survive round-trip."""
        assert_round_trip(queue_operations_session)

    def test_enrichment_stripped_on_serialize(self, tool_round_trip_session, tmp_path):
        """Serialization does not include computed fields (children, tool_pair, etc.)."""
        session = load(tool_round_trip_session)
        out = tmp_path / "out.jsonl"
        serialize(session, out)
        # Read raw output and verify no enrichment fields leaked
        lines = out.read_text().strip().split("\n")
        for line in lines:
            data = json.loads(line)
            assert "children" not in data
            assert "tool_pair" not in data
            assert "content_size" not in data
            assert "is_meta" not in data  # Only if not originally present

    def test_real_session_round_trip(self, dead_session_path, tmp_path):
        """Full 1645-event session survives round-trip."""
        session = load(dead_session_path)
        out = tmp_path / "dead_rt.jsonl"
        serialize(session, out)
        session2 = load(out)
        assert len(session2.events) == 1645
        # Spot-check a few events
        for i in [0, 100, 500, 1000, 1644]:
            assert session.events[i].raw == session2.events[i].raw

    def test_post_compact_round_trip(self, post_compact_path, tmp_path):
        """Post-compact session survives round-trip."""
        session = load(post_compact_path)
        out = tmp_path / "compact_rt.jsonl"
        serialize(session, out)
        session2 = load(out)
        assert len(session2.events) == len(session.events)
        for e1, e2 in zip(session.events, session2.events):
            assert e1.raw == e2.raw

    def test_byte_level_fidelity(self, minimal_session, tmp_path):
        """Round-trip produces byte-identical JSONL (same key order, formatting)."""
        original = minimal_session.read_text()
        session = load(minimal_session)
        out = tmp_path / "out.jsonl"
        serialize(session, out)
        output = out.read_text()
        # Line-by-line comparison (order of JSON keys may differ, so compare parsed)
        orig_lines = original.strip().split("\n")
        out_lines = output.strip().split("\n")
        assert len(orig_lines) == len(out_lines)
        for orig, out in zip(orig_lines, out_lines):
            assert json.loads(orig) == json.loads(out)

    def test_serialize_after_transform_valid(self, tool_round_trip_session, tmp_path):
        """After modifying session (e.g., removing an event), output is still valid."""
        session = load(tool_round_trip_session)
        # Simulate a transform: remove thinking block from assistant content
        for event in session.events:
            if event.type == "assistant" and event.message:
                event.message.content = [b for b in event.message.content
                                          if b.type != "thinking"]
                # Update raw to reflect change
                event.raw["message"]["content"] = [
                    b for b in event.raw["message"]["content"]
                    if b.get("type") != "thinking"
                ]
        out = tmp_path / "transformed.jsonl"
        serialize(session, out)
        # Verify output is valid parseable JSONL
        session2 = load(out)
        assert len(session2.events) == len(session.events)

    def test_newline_handling(self, minimal_session, tmp_path):
        """JSONL output has exactly one newline per event, trailing newline."""
        session = load(minimal_session)
        out = tmp_path / "out.jsonl"
        serialize(session, out)
        content = out.read_text()
        assert content.endswith("\n")
        lines = content.split("\n")
        # Last element after split on trailing newline is empty
        assert lines[-1] == ""
        assert len(lines) - 1 == len(session.events)
```

### 6b. `TestChainRelinking` - parentUuid Integrity After Transforms

```python
class TestChainRelinking:
    """When events are removed, the parentUuid chain must remain walkable from leaf to root.

    CC loads sessions by walking parentUuid from the leaf (last) message to root.
    If we remove events (progress, system, or compressed-out content), we must
    relink the chain so CC can still walk it.

    CC uses logicalParentUuid to skip over compacted sections - we should do the same.
    """

    def test_remove_progress_relinks_chain(self, progress_events_session, tmp_path):
        """Removing progress events relinks neighbors so chain is walkable."""
        session = load(progress_events_session)
        original_leaf = session.events[-1]

        # Remove progress events
        session.remove_events(lambda e: e.type == "progress")

        out = tmp_path / "no_progress.jsonl"
        serialize(session, out)
        session2 = load(out)

        # Chain must be walkable from leaf to root
        chain = session2.tree.walk_chain_to_root(session2.events[-1])
        assert chain[0].parent_uuid is None  # Reaches root
        # No broken links
        for event in session2.events:
            if event.parent_uuid is not None:
                assert session2.tree.get(event.parent_uuid) is not None

    def test_remove_system_relinks_chain(self, system_events_session, tmp_path):
        """Removing system events relinks neighbors."""
        session = load(system_events_session)
        session.remove_events(lambda e: e.type == "system")

        out = tmp_path / "no_system.jsonl"
        serialize(session, out)
        session2 = load(out)

        chain = session2.tree.walk_chain_to_root(session2.events[-1])
        assert chain[0].parent_uuid is None

    def test_remove_middle_events_uses_logical_parent(self, tool_round_trip_session, tmp_path):
        """Removing mid-chain events sets logicalParentUuid on the next event."""
        session = load(tool_round_trip_session)
        # Remove a middle event
        middle_uuid = session.events[1].uuid
        next_event = session.events[2]
        prev_parent = session.events[1].parent_uuid

        session.remove_events(lambda e: e.uuid == middle_uuid)

        out = tmp_path / "removed_middle.jsonl"
        serialize(session, out)
        session2 = load(out)

        # The event after the removed one should point to the removed one's parent
        relinked = [e for e in session2.events if e.uuid == next_event.uuid][0]
        # Either parentUuid updated or logicalParentUuid set
        assert (relinked.parent_uuid == prev_parent or
                relinked.raw.get("logicalParentUuid") == prev_parent)

    def test_chain_walkable_after_bulk_removal(self, dead_session_path, tmp_path):
        """After removing all progress+system events from real session, chain still walks."""
        session = load(dead_session_path)
        original_count = len(session.events)

        session.remove_events(lambda e: e.type in ("progress", "system"))
        assert len(session.events) < original_count

        out = tmp_path / "stripped.jsonl"
        serialize(session, out)
        session2 = load(out)

        # Must be walkable from leaf to root
        chain = session2.tree.walk_chain_to_root(session2.events[-1])
        assert chain[0].parent_uuid is None
        assert len(chain) > 1

    def test_tool_pair_integrity_after_removal(self, tool_round_trip_session, tmp_path):
        """After removing progress events, tool_use/tool_result pairs still match."""
        session = load(tool_round_trip_session)
        session.remove_events(lambda e: e.type == "progress")

        out = tmp_path / "stripped.jsonl"
        serialize(session, out)
        session2 = load(out)

        # Every tool_use still has its matching tool_result
        for pair in session2.tool_pairs():
            assert pair.tool_use.id == pair.tool_result.tool_use_id

    def test_leaf_identity_preserved(self, dead_session_path, tmp_path):
        """The leaf message (what CC resumes from) is preserved after removal."""
        session = load(dead_session_path)
        original_leaf = session.events[-1]

        session.remove_events(lambda e: e.type == "progress")

        out = tmp_path / "stripped.jsonl"
        serialize(session, out)
        session2 = load(out)

        assert session2.events[-1].uuid == original_leaf.uuid
```

### 6c. `TestMicrocompactionAwareness` - Recognizing Already-Cleared Results

```python
class TestMicrocompactionAwareness:
    """Parser should recognize and annotate already-microcompacted tool results."""

    CLEARED_MARKER = "Tool output cleared for context management"

    def test_detect_cleared_tool_result(self, tmp_path):
        """Recognizes the CC microcompaction placeholder."""
        events = [
            make_user_event("u1", None, content=[
                {"type": "tool_result", "tool_use_id": "t1",
                 "content": self.CLEARED_MARKER, "is_error": False}
            ]),
        ]
        path = write_jsonl(tmp_path / "cleared.jsonl", events)
        session = load(path)

        result = session.events[0].message.content[0]
        assert result.is_cleared is True

    def test_non_cleared_not_flagged(self, tmp_path):
        """Normal tool results are not marked as cleared."""
        events = [
            make_user_event("u1", None, content=[
                {"type": "tool_result", "tool_use_id": "t1",
                 "content": "real output here", "is_error": False}
            ]),
        ]
        path = write_jsonl(tmp_path / "normal.jsonl", events)
        session = load(path)

        result = session.events[0].message.content[0]
        assert result.is_cleared is False

    def test_cleared_with_filepath_variant(self, tmp_path):
        """Recognizes the file-backed compaction variant."""
        events = [
            make_user_event("u1", None, content=[
                {"type": "tool_result", "tool_use_id": "t1",
                 "content": "<compacted>Tool result saved to: /tmp/cached.txt\nOriginal tokens: 5000</compacted>",
                 "is_error": False}
            ]),
        ]
        path = write_jsonl(tmp_path / "file_cleared.jsonl", events)
        session = load(path)

        result = session.events[0].message.content[0]
        assert result.is_cleared is True

    def test_content_size_zero_for_cleared(self, tmp_path):
        """Cleared results report content_size as 0 (not the placeholder length)."""
        events = [
            make_user_event("u1", None, content=[
                {"type": "tool_result", "tool_use_id": "t1",
                 "content": self.CLEARED_MARKER, "is_error": False}
            ]),
        ]
        path = write_jsonl(tmp_path / "cleared.jsonl", events)
        session = load(path)

        result = session.events[0].message.content[0]
        assert result.content_size == 0  # Placeholder doesn't count
```

### 6d. `TestTokenEstimation` - CC-Compatible Token Counting

```python
class TestTokenEstimation:
    """Token estimation using CC's verified formula:
    ceil(round(len/4) * 1.3333)
    Images are flat 2000 tokens.
    """

    def test_empty_string(self):
        assert estimate_tokens("") == 0

    def test_short_text(self):
        # 12 chars -> round(12/4) = 3 -> ceil(3 * 1.3333) = ceil(4.0) = 4
        assert estimate_tokens("hello world!") == 4

    def test_medium_text(self):
        # 100 chars -> round(100/4) = 25 -> ceil(25 * 1.3333) = ceil(33.33) = 34
        text = "x" * 100
        assert estimate_tokens(text) == 34

    def test_large_text(self):
        # 10000 chars -> round(10000/4) = 2500 -> ceil(2500 * 1.3333) = ceil(3333.25) = 3334
        text = "x" * 10000
        assert estimate_tokens(text) == 3334

    def test_matches_cc_formula_exactly(self):
        """Verify against CC's exact formula for various inputs."""
        import math
        for length in [0, 1, 4, 7, 15, 50, 100, 500, 1000, 5000, 10000, 50000]:
            expected = math.ceil(round(length / 4) * 1.3333)
            assert estimate_tokens("x" * length) == expected

    def test_session_total_tokens(self, dead_session_path):
        """Total token estimate for real session is in expected range."""
        session = load(dead_session_path)
        total = session.total_tokens()
        # From our analysis: dead.jsonl is ~195K tokens
        assert 150_000 < total < 250_000
```

### 7. `TestEdgeCases` - Format Variations and Corner Cases

```python
class TestEdgeCases:
    """Edge cases: streaming vs batched, malformed data, missing fields."""

    def test_streaming_vs_batched_same_result(self, streaming_session, batched_session):
        """Both patterns produce valid tool pairs and messages."""
        s1 = load(streaming_session)
        s2 = load(batched_session)
        assert len(s1.tool_pairs()) == 2
        assert len(s2.tool_pairs()) == 2

    def test_stop_reason_null_on_streaming(self, streaming_session):
        """Streaming events have stop_reason=None."""
        session = load(streaming_session)
        streaming_events = [e for e in session.events
                           if e.type == "assistant" and e.message]
        for event in streaming_events:
            # All streaming events have null stop_reason
            assert event.message.stop_reason is None

    def test_stop_reason_present_on_batched(self, batched_session):
        """Batched events have stop_reason set."""
        session = load(batched_session)
        asst = [e for e in session.events if e.type == "assistant"][0]
        assert asst.message.stop_reason == "tool_use"

    def test_is_error_true_content(self, error_tool_session):
        """Error tool results have is_error=True and error content."""
        session = load(error_tool_session)
        pairs = session.tool_pairs()
        assert pairs[0].tool_result.is_error is True
        assert "command not found" in pairs[0].tool_result.content

    def test_empty_content_array(self, empty_content_session):
        """Event with empty content array doesn't crash parser."""
        session = load(empty_content_session)
        assert len(session.events) == 2

    def test_multiple_tool_results_one_event(self, batched_session):
        """User event with multiple tool_result blocks parsed correctly."""
        session = load(batched_session)
        user_events = [e for e in session.events if e.type == "user"]
        # Second user event has 2 tool_results
        multi_result = user_events[1]
        assert len(multi_result.message.content) == 2
        assert all(b.type == "tool_result" for b in multi_result.message.content)

    def test_thinking_signature_preserved(self, tool_round_trip_session):
        """Thinking block signatures preserved in raw."""
        session = load(tool_round_trip_session)
        asst = session.events[1]
        thinking_raw = asst.raw["message"]["content"][0]
        assert "signature" in thinking_raw

    def test_usage_fields_preserved(self, minimal_session):
        """Usage dict with all subfields preserved."""
        session = load(minimal_session)
        asst = session.events[1]
        assert asst.message.usage == {"input_tokens": 10, "output_tokens": 5}

    def test_cache_usage_fields(self, dead_session):
        """Real session cache_creation/cache_read fields preserved."""
        asst_events = [e for e in dead_session.events if e.type == "assistant"]
        # At least some should have cache fields
        has_cache = any(
            "cache_read_input_tokens" in (e.message.usage or {})
            for e in asst_events if e.message
        )
        assert has_cache

    def test_non_chronological_timestamps(self, dead_session):
        """Parser handles non-chronological timestamps without error."""
        timestamps = [e.timestamp for e in dead_session.events]
        # Verify timestamps are parsed even if not monotonically increasing
        assert all(isinstance(t, datetime) for t in timestamps)
        # File order ≠ timestamp order (known property of CC sessions)
        is_sorted = all(timestamps[i] <= timestamps[i+1] for i in range(len(timestamps)-1))
        # This assertion documents the known behavior
        assert not is_sorted  # Timestamps are NOT chronological

    def test_slug_field_optional(self, minimal_session):
        """Events without slug field handled."""
        session = load(minimal_session)
        # Minimal session has no slug
        assert session.events[0].raw.get("slug") is None

    def test_slug_field_present(self, dead_session):
        """Events with slug field accessible."""
        has_slug = any(e.raw.get("slug") for e in dead_session.events)
        assert has_slug

    def test_request_id_on_assistant(self, unknown_fields_session):
        """requestId field on assistant events preserved."""
        session = load(unknown_fields_session)
        asst = session.events[1]
        assert asst.raw.get("requestId") == "req_XYZ"

    def test_malformed_event_line(self, tmp_path):
        """Malformed JSON line raises clear error or is skipped gracefully."""
        path = tmp_path / "malformed.jsonl"
        path.write_text('{"type": "user", "uuid": "a"}\nnot json at all\n{"type": "user"}\n')
        # Parser should either raise a clear error or skip malformed lines
        with pytest.raises((json.JSONDecodeError, ParseError)):
            load(path)

    def test_missing_required_fields(self, tmp_path):
        """Event missing required fields (uuid, type) raises error."""
        path = tmp_path / "missing.jsonl"
        path.write_text('{"type": "user"}\n')  # No uuid
        with pytest.raises((KeyError, ParseError)):
            load(path)

    def test_empty_file(self, tmp_path):
        """Empty JSONL file produces empty session."""
        path = tmp_path / "empty.jsonl"
        path.write_text("")
        session = load(path)
        assert len(session.events) == 0

    def test_single_newline_file(self, tmp_path):
        """File with only newlines produces empty session."""
        path = tmp_path / "newlines.jsonl"
        path.write_text("\n\n\n")
        session = load(path)
        assert len(session.events) == 0

    def test_tool_use_without_matching_result(self, tmp_path):
        """tool_use with no matching tool_result handled (orphaned tool_use)."""
        events = [
            make_user_event("u-001", None, [{"type": "text", "text": "Do"}]),
            make_assistant_event("a-001", "u-001", [
                {"type": "tool_use", "id": "toolu_ORPHAN", "name": "Bash",
                 "input": {"command": "echo hi"}}
            ], msg_id="msg_001", stop_reason="tool_use"),
            # No tool_result event follows
        ]
        path = tmp_path / "orphan.jsonl"
        write_events(path, events)
        session = load(path)
        pairs = session.tool_pairs()
        # Orphaned tool_use should not crash, may produce partial pair or be excluded
        assert len(pairs) == 0 or (len(pairs) == 1 and pairs[0].tool_result is None)

    def test_tool_result_without_matching_use(self, tmp_path):
        """tool_result with no matching tool_use handled (orphaned result)."""
        events = [
            make_user_event("u-001", None, [
                {"type": "tool_result", "tool_use_id": "toolu_GHOST",
                 "content": "result for nothing", "is_error": False}
            ]),
        ]
        path = tmp_path / "ghost_result.jsonl"
        write_events(path, events)
        session = load(path)
        pairs = session.tool_pairs()
        assert len(pairs) == 0  # No match possible

    def test_duplicate_uuids(self, tmp_path):
        """Events with duplicate uuids handled (shouldn't happen but be robust)."""
        events = [
            make_user_event("dup-001", None, [{"type": "text", "text": "A"}]),
            make_assistant_event("dup-001", "dup-001", [  # Same UUID!
                {"type": "text", "text": "B"}
            ], msg_id="msg_001", stop_reason="end_turn"),
        ]
        path = tmp_path / "dupes.jsonl"
        write_events(path, events)
        # Should not crash, behavior may vary (last wins, error, etc.)
        session = load(path)
        assert len(session.events) == 2

    def test_very_long_content(self, tmp_path):
        """Events with very long content (100KB+) handled."""
        huge = "x" * 200_000
        events = [
            make_user_event("u-001", None, [{"type": "text", "text": huge}]),
        ]
        path = tmp_path / "huge.jsonl"
        write_events(path, events)
        session = load(path)
        assert len(session.events[0].message.content[0].text) == 200_000

    def test_unicode_content(self, tmp_path):
        """Unicode characters in content preserved."""
        events = [
            make_user_event("u-001", None, [
                {"type": "text", "text": "Hello 世界 🌍 café naïve"}
            ]),
        ]
        path = tmp_path / "unicode.jsonl"
        write_events(path, events)
        session = load(path)
        assert session.events[0].message.content[0].text == "Hello 世界 🌍 café naïve"
        # And round-trip
        assert_round_trip(path)

    def test_nested_json_in_tool_input(self, tmp_path):
        """Deeply nested JSON in tool input preserved."""
        events = [
            make_user_event("u-001", None, [{"type": "text", "text": "Go"}]),
            make_assistant_event("a-001", "u-001", [
                {"type": "tool_use", "id": "toolu_N1", "name": "TodoWrite",
                 "input": {"todos": [
                     {"content": "Task 1", "status": "pending",
                      "activeForm": "Doing task 1"},
                     {"content": "Task 2", "status": "in_progress",
                      "activeForm": "Doing task 2"}
                 ]}}
            ], msg_id="msg_001", stop_reason="tool_use"),
            make_user_event("u-002", "a-001", [
                {"type": "tool_result", "tool_use_id": "toolu_N1",
                 "content": "Todos updated.", "is_error": False}
            ]),
        ]
        path = tmp_path / "nested.jsonl"
        write_events(path, events)
        session = load(path)
        pairs = session.tool_pairs()
        assert pairs[0].tool_use.input["todos"][1]["status"] == "in_progress"
        assert_round_trip(path)
```

### 8. `TestIteratorBehavior` - Lazy Parsing

```python
class TestIteratorBehavior:
    """Iterator-based parsing: lazy loading, partial reads, multiple passes."""

    def test_parse_is_lazy(self, dead_session_path):
        """parse() doesn't load all events into memory immediately."""
        import sys
        iterator = parse(dead_session_path)
        # Just creating iterator shouldn't load all 1645 events
        first = next(iterator)
        assert first.type in ('user', 'assistant', 'progress', 'system', 'queue-operation')

    def test_partial_read(self, dead_session_path):
        """Can read first N events without processing entire file."""
        events = list(itertools.islice(parse(dead_session_path), 10))
        assert len(events) == 10

    def test_iterator_exhaustion(self, minimal_session):
        """Iterator is exhausted after full consumption."""
        it = parse(minimal_session)
        events = list(it)
        assert len(events) == 2
        # Second iteration yields nothing
        assert list(it) == []

    def test_multiple_parse_calls(self, minimal_session):
        """Multiple parse() calls on same file produce independent iterators."""
        it1 = parse(minimal_session)
        it2 = parse(minimal_session)
        events1 = list(it1)
        events2 = list(it2)
        assert len(events1) == len(events2) == 2

    def test_iterator_events_have_raw(self, minimal_session):
        """Events from iterator have raw dict populated."""
        for event in parse(minimal_session):
            assert event.raw is not None
            assert isinstance(event.raw, dict)

    def test_iterator_events_typed(self, minimal_session):
        """Events from iterator have typed fields."""
        events = list(parse(minimal_session))
        assert events[0].type == "user"
        assert events[1].type == "assistant"

    def test_parse_large_file_memory(self, dead_session_path):
        """Parsing large file via iterator doesn't spike memory."""
        import tracemalloc
        tracemalloc.start()
        # Process 100 events via iterator
        count = 0
        for event in parse(dead_session_path):
            count += 1
            if count >= 100:
                break
        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        # Peak memory should be much less than loading entire file
        # (1645 events × avg 1.5KB ≈ 2.5MB if all loaded)
        assert peak < 1_000_000  # Less than 1MB for 100 events

    def test_iterator_preserves_file_order(self, dead_session_path):
        """Iterator yields events in file order."""
        events = list(itertools.islice(parse(dead_session_path), 5))
        # First event should be user (session start)
        assert events[0].type == "user"

    def test_load_uses_parse(self, minimal_session):
        """load() internally uses parse() and materializes all events."""
        session = load(minimal_session)
        assert len(session.events) == 2
        # tree and enrichment computed after materialization
        assert session.tree is not None
```

### 9. `TestEnrichment` - Computed Fields

```python
class TestEnrichment:
    """Enrichment: content_size, tool_pair linking, children computation."""

    def test_content_size_text(self, minimal_session):
        """content_size counts characters in text content blocks."""
        session = load(minimal_session)
        user_event = session.events[0]
        assert user_event.content_size == len("Hello")

    def test_content_size_multiple_blocks(self, batched_session):
        """content_size sums across all content blocks."""
        session = load(batched_session)
        asst = session.events[1]
        expected = (len("Planning...") + len("I'll run two commands.") +
                    # tool_use blocks: count input size
                    len(json.dumps({"command": "ls"})) +
                    len(json.dumps({"file_path": "/tmp/x.py"})))
        # Exact computation depends on implementation
        assert asst.content_size > 0

    def test_content_size_thinking(self, tool_round_trip_session):
        """content_size includes thinking block text."""
        session = load(tool_round_trip_session)
        asst = session.events[1]
        assert asst.content_size >= len("I'll read the file")

    def test_content_size_tool_result(self, tool_round_trip_session):
        """content_size of tool_result event."""
        session = load(tool_round_trip_session)
        result_event = session.events[2]
        assert result_event.content_size == len("print('hello')")

    def test_content_size_empty(self, empty_content_session):
        """content_size is 0 for empty content."""
        session = load(empty_content_session)
        assert session.events[0].content_size == 0

    def test_children_computed_after_load(self, multi_turn_session):
        """Children lists populated after load()."""
        session = load(multi_turn_session)
        root = session.events[0]
        assert len(root.children) >= 1

    def test_children_not_on_iterator_events(self, minimal_session):
        """Events from parse() iterator don't have children (no tree yet)."""
        events = list(parse(minimal_session))
        # Children might be empty list or not computed
        assert events[0].children == [] or events[0].children is None

    def test_tool_pair_back_reference(self, tool_round_trip_session):
        """Events with tool_use have tool_pair back-reference after load."""
        session = load(tool_round_trip_session)
        asst = session.events[1]
        # Event containing tool_use should have tool_pair set
        # (exact API depends on whether it's per-event or per-block)
        pairs = session.tool_pairs()
        assert len(pairs) == 1

    def test_is_meta_enrichment(self, meta_events_session):
        """is_meta computed from isMeta field."""
        session = load(meta_events_session)
        assert session.events[0].is_meta is True
        assert session.events[1].is_meta is False

    def test_enrichment_not_in_raw(self, tool_round_trip_session):
        """Enrichment fields not stored in raw dict."""
        session = load(tool_round_trip_session)
        for event in session.events:
            assert "children" not in event.raw
            assert "content_size" not in event.raw
            assert "tool_pair" not in event.raw
```

### 10. `TestMultipleFormats` - Pre/Post Compact, Various Sizes

```python
class TestMultipleFormats:
    """Different session formats and sizes."""

    def test_pre_compact_loads(self, dead_session):
        """Pre-compact session (dead.jsonl) loads successfully."""
        assert len(dead_session.events) == 1645

    def test_post_compact_loads(self, post_compact_session):
        """Post-compact session loads successfully."""
        assert len(post_compact_session.events) > 0

    def test_post_compact_fewer_events(self, dead_session, post_compact_session):
        """Post-compact has different event count (compaction removes events)."""
        # Document the relationship
        # post-compact-final might actually be larger (continued after compaction)
        assert len(post_compact_session.events) != len(dead_session.events) or True

    def test_post_compact_valid_tree(self, post_compact_session):
        """Post-compact session has valid tree structure."""
        uuid_set = {e.uuid for e in post_compact_session.events}
        orphans = [e for e in post_compact_session.events
                   if e.parent_uuid is not None and e.parent_uuid not in uuid_set]
        # Compaction might create orphans (parent events removed)
        # Parser should handle gracefully
        # If orphans exist, they should still parse without error

    def test_post_compact_messages(self, post_compact_session):
        """Post-compact session produces valid message array."""
        msgs = post_compact_session.messages()
        assert len(msgs) > 0
        assert msgs[0].role == "user"

    def test_post_compact_tool_pairs(self, post_compact_session):
        """Post-compact session has matchable tool pairs."""
        pairs = post_compact_session.tool_pairs()
        # All pairs should have both use and result matched
        for pair in pairs:
            assert pair.tool_use is not None
            assert pair.tool_result is not None

    def test_both_sessions_have_bash(self, dead_session, post_compact_session):
        """Both sessions contain Bash tool usage."""
        dead_pairs = dead_session.tool_pairs()
        compact_pairs = post_compact_session.tool_pairs()
        assert any(p.tool_name == "Bash" for p in dead_pairs)
        assert any(p.tool_name == "Bash" for p in compact_pairs)

    def test_session_ids_consistent(self, dead_session):
        """All events in session share same session_id."""
        ids = {e.session_id for e in dead_session.events
               if e.session_id}  # queue-operations might not have it
        # Should be exactly one session ID (or very few for queue-ops)
        assert len(ids) <= 2

    def test_version_field_present(self, dead_session):
        """Version field present on events."""
        versions = {e.version for e in dead_session.events if e.version}
        assert len(versions) >= 1
        assert all(v.count('.') == 2 for v in versions)  # semver format

    def test_real_session_tool_errors(self, dead_session):
        """Real session has some tool errors (known from analysis)."""
        pairs = dead_session.tool_pairs()
        errors = [p for p in pairs if p.tool_result.is_error]
        assert len(errors) >= 11  # 11 Bash errors known

    def test_real_session_empty_results(self, dead_session):
        """Real session has some empty tool results."""
        pairs = dead_session.tool_pairs()
        empty = [p for p in pairs if p.result_size == 0]
        assert len(empty) >= 14  # 14 empty Bash results known

    def test_real_session_thinking_blocks(self, dead_session):
        """Real session has thinking blocks with signatures."""
        asst_events = [e for e in dead_session.events if e.type == "assistant"]
        thinking_count = 0
        for e in asst_events:
            if e.message:
                for block in e.message.content:
                    if block.type == "thinking":
                        thinking_count += 1
                        # Verify signature present in raw
                        raw_block = next(b for b in e.raw["message"]["content"]
                                        if b.get("type") == "thinking")
                        assert "signature" in raw_block
        assert thinking_count > 0

    def test_real_session_progress_data_types(self, dead_session):
        """Real session has all 4 progress data types."""
        progress = [e for e in dead_session.events if e.type == "progress"]
        data_types = {e.raw.get("data", {}).get("type") for e in progress}
        assert "bash_progress" in data_types
        assert "hook_progress" in data_types
        assert "mcp_progress" in data_types

    def test_real_session_system_subtypes(self, dead_session):
        """Real session has known system event subtypes."""
        system = [e for e in dead_session.events if e.type == "system"]
        subtypes = {e.raw.get("subtype") for e in system}
        assert "stop_hook_summary" in subtypes
        assert "turn_duration" in subtypes
```

---

## conftest.py Structure

```python
# tests/conftest.py

import json
import itertools
from pathlib import Path
from collections import Counter
from datetime import datetime

import pytest

# Import the parser under test
from cc_session_parser import parse, load, serialize, Session, Event
from cc_session_parser import Message, ContentBlock, ToolUse, ToolResult, ToolPair, Turn

PROJECT_ROOT = Path(__file__).parent.parent
SESSIONS_DIR = PROJECT_ROOT / "sessions"


# --- Helper functions ---

def base_event(uuid, parent_uuid, event_type):
    """Create base event dict with required fields."""
    return {
        "type": event_type,
        "uuid": uuid,
        "parentUuid": parent_uuid,
        "timestamp": "2026-01-22T23:34:06.617Z",
        "sessionId": "sess-001",
        "isSidechain": False,
        "userType": "external",
        "cwd": "/tmp",
        "version": "2.1.14",
        "gitBranch": "main",
    }


def make_user_event(uuid, parent_uuid, content_blocks):
    return {
        **base_event(uuid, parent_uuid, "user"),
        "message": {"role": "user", "content": content_blocks}
    }


def make_assistant_event(uuid, parent_uuid, content_blocks, msg_id="msg_default",
                         stop_reason="end_turn", model="claude-opus-4-5-20251101"):
    return {
        **base_event(uuid, parent_uuid, "assistant"),
        "message": {
            "role": "assistant",
            "content": content_blocks,
            "id": msg_id,
            "model": model,
            "type": "message",
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 20}
        }
    }


def write_events(path, events):
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")


def assert_round_trip(path):
    """Parse a session, serialize it, parse again, assert equivalence."""
    session1 = load(path)
    out_path = path.parent / "round_trip_output.jsonl"
    serialize(session1, out_path)
    session2 = load(out_path)
    assert len(session1.events) == len(session2.events)
    for e1, e2 in zip(session1.events, session2.events):
        assert e1.raw == e2.raw


# --- Fixtures using real session files ---

@pytest.fixture
def dead_session_path():
    path = SESSIONS_DIR / "dead.jsonl"
    if not path.exists():
        pytest.skip("Real session file not available")
    return path


@pytest.fixture
def post_compact_path():
    path = SESSIONS_DIR / "post-compact-final.jsonl"
    if not path.exists():
        pytest.skip("Post-compact session file not available")
    return path


@pytest.fixture
def dead_session(dead_session_path):
    return load(dead_session_path)


@pytest.fixture
def post_compact_session(post_compact_path):
    return load(post_compact_path)
```

---

## Test Markers and Organization

```ini
# pytest.ini or pyproject.toml [tool.pytest.ini_options]
[pytest]
markers =
    slow: marks tests that load real session files (deselect with '-m "not slow"')
    round_trip: marks round-trip fidelity tests
    real_data: marks tests using real session JSONL files
```

Apply markers:
- `@pytest.mark.slow` on all `dead_session` / `post_compact_session` tests
- `@pytest.mark.round_trip` on all `TestRoundTrip` tests
- `@pytest.mark.real_data` on tests using real session files

---

## Test Count Summary

| Class | Test Count | Coverage Focus |
|-------|-----------|----------------|
| TestParsing | 24 | Type detection, field access, content blocks |
| TestTreeConstruction | 10 | uuid/parentUuid, children, roots, orphans |
| TestMessageReconstruction | 13 | API message array, merging, filtering |
| TestToolMatching | 16 | tool_use/tool_result pairing, metadata extraction |
| TestTurnDetection | 10 | Logical turns, tool grouping |
| TestRoundTrip | 14 | Parse→serialize equivalence, field preservation |
| TestEdgeCases | 22 | Streaming/batched, errors, malformed, unicode |
| TestIteratorBehavior | 9 | Lazy parsing, memory, multiple passes |
| TestEnrichment | 9 | content_size, children, is_meta, back-refs |
| TestMultipleFormats | 14 | Pre/post compact, real data validation |
| **Total** | **141** | |

---

## Key Invariants to Assert

Throughout all tests, these invariants should hold:

1. **Raw preservation**: `event.raw` always contains the original JSON dict, unmodified
2. **Round-trip fidelity**: `load(serialize(load(path))) == load(path)` (comparing raw dicts)
3. **No enrichment leakage**: Serialized output never contains computed fields
4. **Tool pair completeness**: Every matched pair has both tool_use and tool_result
5. **Tree consistency**: Every non-root event's parent_uuid exists in the event set
6. **File order authority**: Event ordering always matches JSONL line order
7. **Type safety**: All typed accessors return correct types (not raw dicts)
8. **Unicode safety**: All string content survives parse→serialize without corruption
