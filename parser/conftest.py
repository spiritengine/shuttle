"""Test fixtures and helpers for parser tests."""

import json
from pathlib import Path

import pytest

SESSIONS_DIR = Path(__file__).parent.parent / "sessions"


def make_event(uuid, parent_uuid, event_type, message=None, **extra):
    """Create a raw event dict with required fields."""
    event = {
        "type": event_type,
        "uuid": uuid,
        "parentUuid": parent_uuid,
        "timestamp": "2026-01-22T23:34:06.617Z",
        "sessionId": "test-session-001",
        "isSidechain": False,
        "userType": "external",
        "cwd": "/tmp/test",
        "version": "2.1.14",
        "gitBranch": "main",
    }
    if message is not None:
        event["message"] = message
    event.update(extra)
    return event


def make_user_message(content_blocks):
    """Create a user message dict."""
    return {"role": "user", "content": content_blocks}


def make_assistant_message(content_blocks, msg_id=None, stop_reason=None):
    """Create an assistant message dict."""
    msg = {"role": "assistant", "content": content_blocks}
    if msg_id:
        msg["id"] = msg_id
        msg["type"] = "message"
        msg["model"] = "claude-opus-4-5-20251101"
    if stop_reason:
        msg["stop_reason"] = stop_reason
    return msg


def make_tool_use(tool_id, name, tool_input):
    """Create a tool_use content block."""
    return {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}


def make_tool_result(tool_use_id, content, is_error=False):
    """Create a tool_result content block."""
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": content,
        "is_error": is_error,
    }


def make_text(text):
    """Create a text content block."""
    return {"type": "text", "text": text}


def make_thinking(thinking, signature="sig123"):
    """Create a thinking content block."""
    return {"type": "thinking", "thinking": thinking, "signature": signature}


def write_jsonl(path, events):
    """Write a list of raw event dicts to a JSONL file."""
    path = Path(path)
    with open(path, "w") as f:
        for event in events:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    return path


@pytest.fixture
def tmp_jsonl(tmp_path):
    """Factory fixture for creating temp JSONL files."""
    def _write(events, name="test.jsonl"):
        return write_jsonl(tmp_path / name, events)
    return _write


@pytest.fixture
def minimal_events():
    """Minimal valid session: one user + one assistant event."""
    return [
        make_event("u1", None, "user",
                   message=make_user_message([make_text("Hello")])),
        make_event("a1", "u1", "assistant",
                   message=make_assistant_message(
                       [make_thinking("Let me help"), make_text("Hi there!")],
                       msg_id="msg_001", stop_reason="end_turn")),
    ]


@pytest.fixture
def tool_session_events():
    """Session with a tool use cycle: user -> assistant(tool_use) -> user(tool_result) -> assistant(text)."""
    return [
        make_event("u1", None, "user",
                   message=make_user_message([make_text("Read the file")])),
        make_event("a1", "u1", "assistant",
                   message=make_assistant_message(
                       [make_thinking("I'll read it"),
                        make_tool_use("t1", "Read", {"file_path": "/tmp/foo.py"})],
                       msg_id="msg_001", stop_reason="tool_use")),
        make_event("u2", "a1", "user",
                   message=make_user_message(
                       [make_tool_result("t1", "def foo():\n    return 42\n")])),
        make_event("a2", "u2", "assistant",
                   message=make_assistant_message(
                       [make_thinking("The file contains foo"),
                        make_text("The file defines a function `foo` that returns 42.")],
                       msg_id="msg_002", stop_reason="end_turn")),
    ]


@pytest.fixture
def multi_tool_events():
    """Session with multiple parallel tool uses in one assistant response."""
    return [
        make_event("u1", None, "user",
                   message=make_user_message([make_text("Check both files")])),
        make_event("a1", "u1", "assistant",
                   message=make_assistant_message(
                       [make_tool_use("t1", "Read", {"file_path": "/tmp/a.py"}),
                        make_tool_use("t2", "Read", {"file_path": "/tmp/b.py"})],
                       msg_id="msg_001", stop_reason="tool_use")),
        make_event("u2", "a1", "user",
                   message=make_user_message(
                       [make_tool_result("t1", "content of a.py"),
                        make_tool_result("t2", "content of b.py")])),
        make_event("a2", "u2", "assistant",
                   message=make_assistant_message(
                       [make_text("Both files look good.")],
                       msg_id="msg_002", stop_reason="end_turn")),
    ]


@pytest.fixture
def progress_session_events(tool_session_events):
    """Tool session with progress events interspersed."""
    events = list(tool_session_events)
    # Insert progress events between tool_use and tool_result
    progress1 = make_event("p1", "a1", "progress",
                           data={"type": "bash_progress", "output": "line 1",
                                 "fullOutput": "line 1", "elapsedTimeSeconds": 1},
                           toolUseID="bash-progress-0",
                           parentToolUseID="t1")
    progress2 = make_event("p2", "p1", "progress",
                           data={"type": "bash_progress", "output": "line 2",
                                 "fullOutput": "line 1\nline 2", "elapsedTimeSeconds": 2},
                           toolUseID="bash-progress-0",
                           parentToolUseID="t1")
    # Insert after a1 (index 1), before u2 (index 2)
    events.insert(2, progress1)
    events.insert(3, progress2)
    # Fix u2's parentUuid to point to last progress event
    events[4]["parentUuid"] = "p2"
    return events


@pytest.fixture
def system_session_events(tool_session_events):
    """Tool session with system events (hooks, timing)."""
    events = list(tool_session_events)
    system_event = make_event("s1", "a1", "system",
                             subtype="stop_hook_summary",
                             hookCount=1,
                             hookInfos=[{"command": "echo done"}],
                             hookErrors=[],
                             preventedContinuation=False,
                             stopReason="",
                             hasOutput=True,
                             level="suggestion")
    events.insert(2, system_event)
    events[3]["parentUuid"] = "s1"
    return events


@pytest.fixture
def streaming_session_events():
    """Streaming pattern: assistant response split across multiple events."""
    return [
        make_event("u1", None, "user",
                   message=make_user_message([make_text("Hello")])),
        # Streaming: thinking in one event, tool_use in next
        make_event("a1", "u1", "assistant",
                   message=make_assistant_message(
                       [make_thinking("Thinking...")], msg_id="msg_001")),
        make_event("a2", "a1", "assistant",
                   message=make_assistant_message(
                       [make_tool_use("t1", "Bash", {"command": "ls"})],
                       msg_id="msg_001")),
        make_event("u2", "a2", "user",
                   message=make_user_message(
                       [make_tool_result("t1", "file1.py\nfile2.py")])),
        make_event("a3", "u2", "assistant",
                   message=make_assistant_message(
                       [make_text("Found 2 files.")],
                       msg_id="msg_002", stop_reason="end_turn")),
    ]


@pytest.fixture
def meta_session_events():
    """Session with isMeta user events (system injections)."""
    return [
        make_event("m1", None, "user",
                   message={"role": "user",
                            "content": "<local-command-caveat>Caveat text</local-command-caveat>"},
                   isMeta=True),
        make_event("u1", "m1", "user",
                   message=make_user_message([make_text("Real user message")])),
        make_event("a1", "u1", "assistant",
                   message=make_assistant_message(
                       [make_text("Response")],
                       msg_id="msg_001", stop_reason="end_turn")),
    ]


@pytest.fixture
def cleared_session_events():
    """Session with already-microcompacted tool results."""
    return [
        make_event("u1", None, "user",
                   message=make_user_message([make_text("Do stuff")])),
        make_event("a1", "u1", "assistant",
                   message=make_assistant_message(
                       [make_tool_use("t1", "Read", {"file_path": "/tmp/big.py"})],
                       msg_id="msg_001", stop_reason="tool_use")),
        make_event("u2", "a1", "user",
                   message=make_user_message(
                       [make_tool_result("t1", "Tool output cleared for context management")])),
        make_event("a2", "u2", "assistant",
                   message=make_assistant_message(
                       [make_text("I see the file was cleared.")],
                       msg_id="msg_002", stop_reason="end_turn")),
    ]


@pytest.fixture
def dead_session_path():
    """Path to the full 1645-event test session."""
    path = SESSIONS_DIR / "dead.jsonl"
    if not path.exists():
        pytest.skip("dead.jsonl not available")
    return path


@pytest.fixture
def post_compact_path():
    """Path to the post-compaction test session."""
    path = SESSIONS_DIR / "post-compact-final.jsonl"
    if not path.exists():
        pytest.skip("post-compact-final.jsonl not available")
    return path
