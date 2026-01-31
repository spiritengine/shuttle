"""Type definitions for CC session JSONL parser."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

# CC's microcompaction placeholder
CLEARED_MARKER = "Tool output cleared for context management"
CLEARED_FILE_PREFIX = "<compacted>Tool result saved to:"

EventType = Literal["user", "assistant", "progress", "system", "queue-operation"]


def estimate_tokens(text: str) -> int:
    """CC-compatible token estimation: ceil(round(len/4) * 1.3333).

    Images are flat 2000 tokens (handled separately).
    """
    if not text:
        return 0
    return math.ceil(round(len(text) / 4) * 1.3333)


@dataclass
class ContentBlock:
    """Base content block - thin wrapper preserving the raw dict."""

    raw: dict
    type: str

    @property
    def is_text(self) -> bool:
        return self.type == "text"

    @property
    def is_thinking(self) -> bool:
        return self.type == "thinking"

    @property
    def is_tool_use(self) -> bool:
        return self.type == "tool_use"

    @property
    def is_tool_result(self) -> bool:
        return self.type == "tool_result"

    # Text block properties
    @property
    def text(self) -> str | None:
        if self.type == "text":
            return self.raw.get("text")
        return None

    # Thinking block properties
    @property
    def thinking(self) -> str | None:
        if self.type == "thinking":
            return self.raw.get("thinking")
        return None

    @property
    def signature(self) -> str | None:
        if self.type == "thinking":
            return self.raw.get("signature")
        return None

    # Tool use properties
    @property
    def tool_use_id(self) -> str | None:
        if self.type == "tool_use":
            return self.raw.get("id")
        if self.type == "tool_result":
            return self.raw.get("tool_use_id")
        return None

    @property
    def name(self) -> str | None:
        if self.type == "tool_use":
            return self.raw.get("name")
        return None

    @property
    def input(self) -> dict | None:
        if self.type == "tool_use":
            return self.raw.get("input")
        return None

    # Tool result properties
    @property
    def content(self) -> str | None:
        if self.type == "tool_result":
            c = self.raw.get("content", "")
            if isinstance(c, str):
                return c
            if isinstance(c, list):
                text_parts = []
                for block in c:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                return "".join(text_parts)
            return ""
        return None

    @property
    def is_error(self) -> bool:
        if self.type == "tool_result":
            return self.raw.get("is_error", False)
        return False

    @property
    def is_cleared(self) -> bool:
        """Whether this tool result has been microcompacted by CC."""
        if self.type != "tool_result":
            return False
        c = self.content
        if not c:
            return False
        return c == CLEARED_MARKER or c.startswith(CLEARED_FILE_PREFIX)

    @property
    def content_size(self) -> int:
        """Size in chars of meaningful content (0 for cleared results)."""
        if self.type == "text":
            return len(self.raw.get("text", ""))
        if self.type == "thinking":
            return len(self.raw.get("thinking", ""))
        if self.type == "tool_use":
            return len(json.dumps(self.raw.get("input", {})))
        if self.type == "tool_result":
            if self.is_cleared:
                return 0
            c = self.content
            return len(c) if c else 0
        return 0

    @property
    def tokens(self) -> int:
        """Estimated tokens using CC's formula."""
        if self.type == "text":
            return estimate_tokens(self.raw.get("text", ""))
        if self.type == "thinking":
            return estimate_tokens(self.raw.get("thinking", ""))
        if self.type == "tool_use":
            return estimate_tokens(json.dumps(self.raw.get("input", {})))
        if self.type == "tool_result":
            c = self.content
            return estimate_tokens(c) if c else 0
        return 0


@dataclass
class Message:
    """A message within an event."""

    raw: dict
    role: str
    content: list[ContentBlock]

    # Assistant-only fields
    @property
    def id(self) -> str | None:
        return self.raw.get("id")

    @property
    def model(self) -> str | None:
        return self.raw.get("model")

    @property
    def stop_reason(self) -> str | None:
        return self.raw.get("stop_reason")

    @property
    def usage(self) -> dict | None:
        return self.raw.get("usage")

    @property
    def content_size(self) -> int:
        return sum(b.content_size for b in self.content)

    @property
    def tokens(self) -> int:
        return sum(b.tokens for b in self.content)


@dataclass
class Event:
    """A single JSONL event, preserving all raw data for round-trip."""

    raw: dict

    # Core fields (always present)
    uuid: str
    parent_uuid: str | None
    type: EventType
    timestamp: str
    session_id: str
    is_sidechain: bool
    cwd: str
    git_branch: str
    version: str

    # Parsed message (if present)
    message: Message | None

    # Enrichment (computed, not serialized)
    children: list[Event] = field(default_factory=list, repr=False)

    @property
    def is_meta(self) -> bool:
        return self.raw.get("isMeta", False)

    @property
    def is_conversation(self) -> bool:
        """Whether this event participates in the API conversation."""
        if self.is_sidechain:
            return False
        if self.type in ("progress", "system", "queue-operation"):
            return False
        return True

    @property
    def tool_use_result(self) -> dict | None:
        """Top-level toolUseResult (duplicate of message content, has stdout/stderr split)."""
        return self.raw.get("toolUseResult")

    @property
    def slug(self) -> str | None:
        return self.raw.get("slug")

    @property
    def request_id(self) -> str | None:
        return self.raw.get("requestId")

    @property
    def message_id(self) -> str | None:
        """API message ID (groups events from one API response)."""
        if self.message and self.message.raw:
            return self.message.raw.get("id")
        return None

    @property
    def content_size(self) -> int:
        if self.message:
            return self.message.content_size
        return 0

    @property
    def tokens(self) -> int:
        if self.message:
            return self.message.tokens
        return 0

    @property
    def subtype(self) -> str | None:
        return self.raw.get("subtype")

    @property
    def logical_parent_uuid(self) -> str | None:
        return self.raw.get("logicalParentUuid")


@dataclass
class ToolPair:
    """A matched tool_use + tool_result pair."""

    tool_use_event: Event
    tool_result_event: Event
    tool_use_block: ContentBlock
    tool_result_block: ContentBlock

    @property
    def tool_name(self) -> str:
        return self.tool_use_block.name or "unknown"

    @property
    def tool_use_id(self) -> str:
        return self.tool_use_block.tool_use_id or ""

    @property
    def result_size(self) -> int:
        return self.tool_result_block.content_size

    @property
    def result_tokens(self) -> int:
        return self.tool_result_block.tokens

    @property
    def is_cleared(self) -> bool:
        return self.tool_result_block.is_cleared

    @property
    def file_path(self) -> str | None:
        """For Read/Write/Edit tools."""
        inp = self.tool_use_block.input
        if inp and self.tool_name in ("Read", "Write", "Edit"):
            return inp.get("file_path")
        return None

    @property
    def command(self) -> str | None:
        """For Bash tool."""
        inp = self.tool_use_block.input
        if inp and self.tool_name == "Bash":
            return inp.get("command")
        return None

    @property
    def pattern(self) -> str | None:
        """For Grep/Glob tools."""
        inp = self.tool_use_block.input
        if inp and self.tool_name in ("Grep", "Glob"):
            return inp.get("pattern")
        return None
