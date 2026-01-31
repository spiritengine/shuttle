"""Enrichment: tool pair matching and per-tool metadata extraction."""

from __future__ import annotations

from .models import ContentBlock, Event, ToolPair


def match_tool_pairs(events: list[Event]) -> list[ToolPair]:
    """Match tool_use blocks to their corresponding tool_result blocks.

    tool_use blocks appear in assistant messages with an 'id' field.
    tool_result blocks appear in user messages with a 'tool_use_id' field that matches.
    """
    # Collect all tool_use blocks with their events
    tool_uses: dict[str, tuple[Event, ContentBlock]] = {}
    for event in events:
        if event.type != "assistant" or not event.message:
            continue
        for block in event.message.content:
            if block.is_tool_use and block.tool_use_id:
                tool_uses[block.tool_use_id] = (event, block)

    # Match with tool_result blocks
    pairs: list[ToolPair] = []
    for event in events:
        if event.type != "user" or not event.message:
            continue
        for block in event.message.content:
            if block.is_tool_result and block.tool_use_id:
                use_entry = tool_uses.get(block.tool_use_id)
                if use_entry:
                    use_event, use_block = use_entry
                    pairs.append(ToolPair(
                        tool_use_event=use_event,
                        tool_result_event=event,
                        tool_use_block=use_block,
                        tool_result_block=block,
                    ))

    return pairs
