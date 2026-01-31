"""CC Session JSONL Parser.

Parse Claude Code session JSONL files into typed data models
with round-trip serialization support.

Usage:
    from parser import load, serialize, Session

    session = load("path/to/session.jsonl")
    print(f"{len(session.events)} events, {session.total_tokens()} tokens")

    for pair in session.tool_pairs():
        print(f"{pair.tool_name}: {pair.result_size} chars")

    session.remove_events(lambda e: e.type == "progress")
    serialize(session, "output.jsonl")
"""

from .models import (
    CLEARED_FILE_PREFIX,
    CLEARED_MARKER,
    ContentBlock,
    Event,
    Message,
    ToolPair,
    estimate_tokens,
)
from .parse import load, parse
from .serialize import relink_chain, serialize, serialize_to_bytes
from .session import Session
from .tree import EventTree

__all__ = [
    "CLEARED_FILE_PREFIX",
    "CLEARED_MARKER",
    "ContentBlock",
    "Event",
    "EventTree",
    "Message",
    "Session",
    "ToolPair",
    "estimate_tokens",
    "load",
    "parse",
    "relink_chain",
    "serialize",
    "serialize_to_bytes",
]
