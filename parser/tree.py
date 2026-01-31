"""Event tree construction and traversal.

CC sessions form a tree via uuid/parentUuid relationships.
The tree is used for:
- Finding roots (events with no parent)
- Walking chains from leaf to root (how CC loads sessions)
- Computing children for each event
- Detecting parallel tool result branches
"""

from __future__ import annotations

from collections import defaultdict

from .models import Event


class EventTree:
    """Tree structure built from event uuid/parentUuid relationships."""

    def __init__(self, events: list[Event]):
        self._by_uuid: dict[str, Event] = {}
        self._children: dict[str, list[Event]] = defaultdict(list)
        self._roots: list[Event] = []

        self._build(events)

    def _build(self, events: list[Event]) -> None:
        """Build the tree index from a list of events."""
        # Index by UUID
        for event in events:
            if event.uuid:
                self._by_uuid[event.uuid] = event

        # Build parent-child relationships
        for event in events:
            if event.parent_uuid is None:
                self._roots.append(event)
            else:
                self._children[event.parent_uuid].append(event)

        # Populate children on each event
        for event in events:
            event.children = self._children.get(event.uuid, [])

    @property
    def roots(self) -> list[Event]:
        """Events with no parent (conversation roots)."""
        return self._roots

    def get(self, uuid: str) -> Event | None:
        """Get an event by UUID."""
        return self._by_uuid.get(uuid)

    def children_of(self, uuid: str) -> list[Event]:
        """Get direct children of an event."""
        return self._children.get(uuid, [])

    def walk_chain_to_root(self, leaf: Event) -> list[Event]:
        """Walk parentUuid chain from leaf to root.

        This is how CC loads sessions: start from the last event,
        walk backwards via parentUuid until reaching null parent.

        Uses logicalParentUuid as fallback when parentUuid target doesn't exist
        (happens after compaction or event removal).

        Returns list from root to leaf (reversed walk order).
        """
        chain = []
        seen = set()
        current = leaf

        while current is not None:
            if current.uuid in seen:
                break  # Cycle detection
            seen.add(current.uuid)
            chain.append(current)

            # Try parentUuid first, fall back to logicalParentUuid
            next_uuid = current.parent_uuid
            next_event = self._by_uuid.get(next_uuid) if next_uuid else None

            if next_event is None and current.logical_parent_uuid:
                next_event = self._by_uuid.get(current.logical_parent_uuid)

            current = next_event

        chain.reverse()  # Root first
        return chain

    def rebuild(self, events: list[Event]) -> None:
        """Rebuild the tree from a new event list (after transforms)."""
        self._by_uuid.clear()
        self._children.clear()
        self._roots.clear()
        for event in events:
            event.children = []
        self._build(events)
