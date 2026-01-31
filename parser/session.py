"""Session: the top-level container for a parsed CC session."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from .enrich import match_tool_pairs
from .models import Event, ToolPair, estimate_tokens
from .parse import parse
from .tree import EventTree


class Session:
    """A fully parsed CC session with tree structure and enrichment.

    Provides access to:
    - events: ordered list of all events
    - tree: parent-child relationships
    - tool_pairs(): matched tool_use + tool_result pairs
    - messages(): reconstructed API message array
    - total_tokens(): estimated token count
    - remove_events(): remove events with chain relinking support
    """

    def __init__(self, events: list[Event]):
        self.events = events
        self._original_events: list[Event] = list(events)  # Snapshot for chain relinking
        self.tree = EventTree(events)
        self._tool_pairs: list[ToolPair] | None = None

    @classmethod
    def from_path(cls, path: Path | str) -> "Session":
        """Load a session from a JSONL file path."""
        events = list(parse(path))
        return cls(events)

    @classmethod
    def from_events(cls, events: list[Event]) -> "Session":
        """Create a session from a list of already-parsed events."""
        return cls(events)

    def tool_pairs(self) -> list[ToolPair]:
        """Get all matched tool_use/tool_result pairs."""
        if self._tool_pairs is None:
            self._tool_pairs = match_tool_pairs(self.events)
        return self._tool_pairs

    def messages(self) -> list[dict]:
        """Reconstruct the API message array from events.

        Merges consecutive same-role events (from streaming responses)
        and filters out non-conversational events (progress, system, sidechains).

        Returns list of {"role": str, "content": list[dict]} dicts.
        """
        result: list[dict] = []

        for event in self.events:
            if not event.is_conversation:
                continue
            if not event.message:
                continue

            role = event.message.role
            content_blocks = [b.raw for b in event.message.content]

            if not content_blocks:
                continue

            # Merge consecutive same-role messages
            if result and result[-1]["role"] == role:
                result[-1]["content"].extend(content_blocks)
            else:
                result.append({
                    "role": role,
                    "content": list(content_blocks),
                })

        return result

    def conversation_events(self) -> list[Event]:
        """Get only events that participate in the API conversation."""
        return [e for e in self.events if e.is_conversation]

    def total_tokens(self) -> int:
        """Estimate total tokens in the session using CC's formula."""
        total = 0
        for event in self.events:
            if event.is_conversation and event.message:
                total += event.message.tokens
        return total

    def events_by_type(self) -> dict[str, list[Event]]:
        """Group events by their type."""
        groups: dict[str, list[Event]] = {}
        for event in self.events:
            groups.setdefault(event.type, []).append(event)
        return groups

    def events_by_message_id(self) -> dict[str, list[Event]]:
        """Group assistant events by their API message ID."""
        groups: dict[str, list[Event]] = {}
        for event in self.events:
            msg_id = event.message_id
            if msg_id:
                groups.setdefault(msg_id, []).append(event)
        return groups

    def remove_events(self, predicate: Callable[[Event], bool]) -> None:
        """Remove events matching predicate.

        After removal, the parentUuid chain may have dangling references.
        These will be fixed during serialization (relink_chain).
        Invalidates cached tool_pairs.
        """
        self.events = [e for e in self.events if not predicate(e)]
        self._tool_pairs = None
        self.tree.rebuild(self.events)
