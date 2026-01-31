"""Serialize a Session back to valid CC JSONL.

Critical requirement: output must be loadable by CC.
CC loads sessions by walking parentUuid from leaf to root.
If events have been removed, the chain must be relinked.
"""

from __future__ import annotations

import json
from pathlib import Path

from .models import Event


def relink_chain(events: list[Event], original_events: list[Event] | None = None) -> None:
    """Relink parentUuid chain after events have been removed.

    Walks up the ORIGINAL parentUuid chain to find the nearest ancestor
    that still survives in the filtered event list. Sets logicalParentUuid
    to preserve the original link for debugging.

    Args:
        events: The filtered event list (after removal).
        original_events: The full original event list (before removal).
            If not provided, falls back to file-order heuristic.

    Mutates events in-place (updates their raw dicts).
    """
    uuid_set = {e.uuid for e in events}

    # Build index of original events for chain walking
    original_by_uuid: dict[str, Event] = {}
    if original_events:
        original_by_uuid = {e.uuid: e for e in original_events}

    for event in events:
        if event.parent_uuid is None:
            continue
        if event.parent_uuid in uuid_set:
            continue

        # Parent was removed - preserve original link
        event.raw["logicalParentUuid"] = event.parent_uuid

        # Walk up the original chain to find nearest surviving ancestor
        ancestor_uuid = None
        if original_by_uuid:
            current_uuid = event.parent_uuid
            seen = set()
            while current_uuid and current_uuid not in seen:
                seen.add(current_uuid)
                if current_uuid in uuid_set:
                    ancestor_uuid = current_uuid
                    break
                orig = original_by_uuid.get(current_uuid)
                if orig:
                    current_uuid = orig.parent_uuid
                else:
                    break

        if ancestor_uuid is None:
            # Fallback: find nearest preceding event in file order
            idx = next((i for i, e in enumerate(events) if e.uuid == event.uuid), 0)
            if idx > 0:
                ancestor_uuid = events[idx - 1].uuid

        if ancestor_uuid:
            event.raw["parentUuid"] = ancestor_uuid
            event.parent_uuid = ancestor_uuid
        else:
            # No ancestor found - promote to root
            event.raw["parentUuid"] = None
            event.parent_uuid = None


def _needs_relink(session: "Session") -> bool:
    """Check if any events have dangling parentUuid references."""
    uuid_set = {e.uuid for e in session.events}
    return any(
        e.parent_uuid is not None and e.parent_uuid not in uuid_set
        for e in session.events
    )


def serialize(session: "Session", path: Path | str) -> None:
    """Serialize a Session to a JSONL file.

    Writes events in order, one JSON object per line.
    Strips computed enrichment fields (children, etc).
    If events have been removed, relinks the parentUuid chain first.
    """
    path = Path(path)

    if _needs_relink(session):
        relink_chain(session.events, session._original_events)

    with open(path, "w") as f:
        for event in session.events:
            f.write(json.dumps(event.raw, ensure_ascii=False))
            f.write("\n")


def serialize_to_bytes(session: "Session") -> bytes:
    """Serialize a Session to bytes (for testing without files)."""
    if _needs_relink(session):
        relink_chain(session.events, session._original_events)

    lines = []
    for event in session.events:
        lines.append(json.dumps(event.raw, ensure_ascii=False))
    return ("\n".join(lines) + "\n").encode("utf-8")
