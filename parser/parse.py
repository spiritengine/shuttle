"""Parse CC session JSONL files into typed Event models."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from .models import ContentBlock, Event, Message


def parse_content_block(raw: dict) -> ContentBlock:
    """Parse a single content block from a message."""
    return ContentBlock(raw=raw, type=raw.get("type", "unknown"))


def parse_message(raw: dict) -> Message:
    """Parse a message dict into a Message object."""
    role = raw.get("role", "unknown")
    raw_content = raw.get("content", [])

    if isinstance(raw_content, str):
        # isMeta events can have string content
        blocks = [ContentBlock(raw={"type": "text", "text": raw_content}, type="text")]
    elif isinstance(raw_content, list):
        blocks = [parse_content_block(b) for b in raw_content if isinstance(b, dict)]
    else:
        blocks = []

    return Message(raw=raw, role=role, content=blocks)


def parse_event(raw: dict) -> Event:
    """Parse a raw JSON dict into a typed Event."""
    message = None
    msg_raw = raw.get("message")
    if msg_raw and isinstance(msg_raw, dict):
        message = parse_message(msg_raw)

    # Normalize empty string parentUuid to None (CC uses both)
    parent_uuid = raw.get("parentUuid")
    if parent_uuid == "":
        parent_uuid = None

    return Event(
        raw=raw,
        uuid=raw.get("uuid", ""),
        parent_uuid=parent_uuid,
        type=raw.get("type", "unknown"),
        timestamp=raw.get("timestamp", ""),
        session_id=raw.get("sessionId", ""),
        is_sidechain=raw.get("isSidechain", False),
        cwd=raw.get("cwd", ""),
        git_branch=raw.get("gitBranch", ""),
        version=raw.get("version", ""),
        message=message,
    )


def parse(path: Path | str) -> Iterator[Event]:
    """Parse a CC session JSONL file, yielding Event objects.

    Iterator-based: memory-efficient for large sessions.
    Events are yielded in file order (which is authoritative, not timestamps).
    """
    path = Path(path)
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            raw = json.loads(line)
            yield parse_event(raw)


def load(path: Path | str) -> "Session":
    """Load a full session from JSONL, building tree and enrichment.

    Returns a Session object with all events loaded, tree built,
    and tool pairs matched.
    """
    from .session import Session
    return Session.from_path(path)
