"""Score and rank CC sessions by interactivity and signal density.

Scans all CC session JSONL files and produces a ranked manifest of
interactive sessions — ones where a human was genuinely engaged,
not just automated spindle/shard tasks running headlessly.

Signal density = meaningful human+Claude dialogue content per unit of
session activity. High-signal sessions have: frequent human turns,
substantive responses, diverse productive tool use.

Usage:
    python3 parser/score_sessions.py [options]
    shuttle score-sessions [options]

Options:
    --project, -p PROJECT   Filter by project name (substring)
    --since, -s DAYS        Only sessions from last N days
    --limit, -n N           Limit results (default: 20)
    --all, -a               All projects (default: filter to cwd project)
    --json, -j              JSON output
    --min-score M           Only sessions scoring at or above M (0-100)
    --include-automated     Include automated sessions (default: interactive only)
    --verbose, -v           Show progress while scanning
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator


# Tools that indicate productive file-editing work
EDIT_TOOLS = frozenset({"Write", "Edit", "NotebookEdit", "MultiEdit"})

# Tools that are high-value signal (code/file work + search)
SIGNAL_TOOLS = frozenset({
    "Bash", "Read", "Write", "Edit", "Glob", "Grep",
    "NotebookEdit", "MultiEdit", "WebFetch", "WebSearch",
})

# Patterns in the first user message that flag an automated session
AUTOMATED_MARKERS = [
    "HANDOFF:",
    "## Previous Task Output",
    "You are working in an isolated SHARD",
    "Before starting work, orient yourself",
    "skein ignite",
    "spool-",
    "This is a task from the Spindle",
]


def _is_automated_message(text: str) -> bool:
    """Heuristically detect if a message is from an automated dispatcher."""
    if len(text) > 800:
        # Long structured prompts are usually automated task dispatches
        # Check for automation markers in the first 500 chars
        prefix = text[:500]
        if any(marker in prefix for marker in AUTOMATED_MARKERS):
            return True
    return any(marker in text for marker in AUTOMATED_MARKERS)


@dataclass
class SessionScore:
    """Scored session with all metrics."""
    session_id: str
    project_path: str
    file_path: str
    last_activity: str | None
    file_mtime: float

    # Raw metrics
    total_events: int
    human_text_turns: int          # user events with string content
    human_text_chars: int          # total chars from human string messages
    assistant_text_chars: int      # total chars from assistant text blocks
    thinking_chars: int            # total chars from thinking blocks
    tool_call_count: int           # total tool_use blocks
    unique_tools: list[str]        # distinct tool names used
    edit_tool_count: int           # Write/Edit/NotebookEdit uses
    first_human_message: str       # preview of first human text
    session_duration_minutes: float | None

    # Derived
    score: float                   # 0-100 signal density score
    is_interactive: bool           # True if human was genuinely engaged


def score_session_file(path: Path) -> SessionScore | None:
    """Score a single session JSONL file.

    Uses lightweight streaming JSON parsing — no full tree construction.
    Returns None if file is empty, unreadable, or has no meaningful content.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        return None

    # Metrics we accumulate
    total_events = 0
    human_text_turns = 0
    human_text_chars = 0
    assistant_text_chars = 0
    thinking_chars = 0
    tool_call_count = 0
    unique_tool_names: set[str] = set()
    edit_tool_count = 0
    first_human_message = ""
    first_timestamp: str | None = None
    last_timestamp: str | None = None
    # Track whether first human message looks automated
    first_human_is_automated = False

    for raw_line in lines:
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue

        total_events += 1
        event_type = event.get("type", "")

        # Track timestamps for duration
        ts = event.get("timestamp", "")
        if ts:
            if first_timestamp is None:
                first_timestamp = ts
            last_timestamp = ts

        msg = event.get("message") or {}
        if not msg:
            continue

        role = msg.get("role", "")
        content = msg.get("content")

        if event_type == "user" and role == "user":
            # Human's typed messages appear as string content
            # Tool results appear as list content
            if isinstance(content, str) and content.strip():
                text_content = content.strip()
                # Filter out CC system messages
                if text_content.startswith("[Request interrupted"):
                    continue
                if text_content.startswith("<local-command"):
                    continue

                human_text_turns += 1
                human_text_chars += len(text_content)

                if not first_human_message:
                    first_human_message = text_content[:120]
                    first_human_is_automated = _is_automated_message(text_content)

        elif event_type == "assistant" and role == "assistant":
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type", "")
                    if btype == "text":
                        txt = block.get("text", "") or ""
                        assistant_text_chars += len(txt)
                    elif btype == "thinking":
                        thinking = block.get("thinking", "") or ""
                        thinking_chars += len(thinking)
                    elif btype == "tool_use":
                        tool_name = block.get("name", "unknown")
                        tool_call_count += 1
                        unique_tool_names.add(tool_name)
                        if tool_name in EDIT_TOOLS:
                            edit_tool_count += 1

    if total_events == 0:
        return None

    # Compute duration
    duration_minutes: float | None = None
    if first_timestamp and last_timestamp and first_timestamp != last_timestamp:
        try:
            # ISO 8601 timestamps
            t0 = datetime.fromisoformat(first_timestamp.replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(last_timestamp.replace("Z", "+00:00"))
            duration_minutes = (t1 - t0).total_seconds() / 60.0
        except (ValueError, OverflowError):
            pass

    # Determine project path
    project_path = _decode_project_path(path.parent.name)

    # Last activity from file mtime or last timestamp
    file_mtime = path.stat().st_mtime
    last_activity: str | None = None
    if last_timestamp:
        last_activity = last_timestamp
    else:
        last_activity = datetime.fromtimestamp(file_mtime).isoformat()

    # Is this an interactive session?
    # Interactive = human typed multiple messages (not just one automated task)
    # OR human typed one non-automated message
    is_interactive = (
        human_text_turns > 1
        or (human_text_turns == 1 and not first_human_is_automated)
    )

    # Signal density score
    score = _compute_score(
        human_text_turns=human_text_turns,
        human_text_chars=human_text_chars,
        assistant_text_chars=assistant_text_chars,
        tool_call_count=tool_call_count,
        unique_tools=unique_tool_names,
        edit_tool_count=edit_tool_count,
        total_events=total_events,
        duration_minutes=duration_minutes,
        is_interactive=is_interactive,
    )

    return SessionScore(
        session_id=path.stem,
        project_path=project_path,
        file_path=str(path),
        last_activity=last_activity,
        file_mtime=file_mtime,
        total_events=total_events,
        human_text_turns=human_text_turns,
        human_text_chars=human_text_chars,
        assistant_text_chars=assistant_text_chars,
        thinking_chars=thinking_chars,
        tool_call_count=tool_call_count,
        unique_tools=sorted(unique_tool_names),
        edit_tool_count=edit_tool_count,
        first_human_message=first_human_message,
        session_duration_minutes=duration_minutes,
        score=score,
        is_interactive=is_interactive,
    )


def _decode_project_path(encoded: str) -> str:
    """Decode project path from CC directory name.

    '-home-patrick-projects-speakbot' → '/home/patrick/projects/speakbot'
    """
    return "/" + encoded.lstrip("-").replace("-", "/")


def _compute_score(
    *,
    human_text_turns: int,
    human_text_chars: int,
    assistant_text_chars: int,
    tool_call_count: int,
    unique_tools: set[str],
    edit_tool_count: int,
    total_events: int,
    duration_minutes: float | None,
    is_interactive: bool,
) -> float:
    """Compute a signal density score (raw, pre-normalization).

    Signal density measures: how much meaningful human+Claude dialogue
    happened per unit of session activity. Not just volume, but quality
    of engagement.

    Components:
    - Human engagement: how actively the human participated
    - Work density: productive tool use relative to session size
    - Content richness: substantive responses vs noise
    """
    if not is_interactive:
        # Automated sessions get scored too but at a baseline
        # This lets --include-automated show them in context
        base = 0.0
    else:
        base = 10.0  # Base credit for being interactive

    # Human engagement (0-40 pts)
    # More human turns = more interactive. Log-scale so 10 turns >> 2 turns,
    # but 100 turns doesn't dominate 20 turns.
    human_engagement = min(math.log1p(human_text_turns) * 15, 40.0)

    # Human message depth (0-15 pts)
    # Longer human messages = richer context. Cap at a reasonable level.
    avg_human_msg_len = human_text_chars / max(human_text_turns, 1)
    message_depth = min(math.log1p(avg_human_msg_len / 20) * 7, 15.0)

    # Tool diversity (0-10 pts)
    # Using many different tools = exploring diverse tasks
    tool_diversity = min(len(unique_tools) * 1.0, 10.0)

    # Productive edits (0-15 pts)
    # File editing = real work happened. Log-scale.
    edit_signal = min(math.log1p(edit_tool_count) * 6, 15.0)

    # Response substance (0-10 pts)
    # High assistant text per event = substantive responses
    # But only relevant for interactive sessions where responses add value
    if total_events > 0:
        text_density = assistant_text_chars / total_events
        response_substance = min(math.log1p(text_density / 100) * 5, 10.0)
    else:
        response_substance = 0.0

    # Session scale bonus (0-10 pts)
    # Bigger sessions (more events) get a small bonus for scope
    scale_bonus = min(math.log1p(total_events / 20) * 3, 10.0)

    raw = base + human_engagement + message_depth + tool_diversity + edit_signal + response_substance + scale_bonus
    return round(raw, 1)


def _normalize_scores(sessions: list[SessionScore]) -> list[SessionScore]:
    """Normalize scores to 0-100 range across the batch.

    Uses max-normalization: highest raw score maps to 100.
    This makes scores relative to the best session in the batch.
    """
    if not sessions:
        return sessions
    max_score = max(s.score for s in sessions)
    if max_score <= 0:
        return sessions
    for s in sessions:
        s.score = round((s.score / max_score) * 100, 1)
    return sessions


def walk_session_files(
    project_filter: str | None = None,
    since_days: int | None = None,
) -> Iterator[Path]:
    """Walk CC session files, applying filters.

    Args:
        project_filter: Substring to match against decoded project path.
        since_days: Only files modified in the last N days.

    Yields:
        Paths to session JSONL files (excluding agent- subfiles).
    """
    cc_projects = Path.home() / ".claude" / "projects"
    if not cc_projects.exists():
        return

    cutoff_ts: float | None = None
    if since_days is not None:
        cutoff_ts = (datetime.now() - timedelta(days=since_days)).timestamp()

    for project_dir in sorted(cc_projects.iterdir()):
        if not project_dir.is_dir():
            continue

        # Apply project filter (on decoded path)
        if project_filter:
            decoded = _decode_project_path(project_dir.name)
            if project_filter.lower() not in decoded.lower():
                continue

        for file in sorted(project_dir.glob("*.jsonl")):
            # Skip agent subfiles
            if file.name.startswith("agent-"):
                continue

            # Apply mtime filter
            if cutoff_ts is not None:
                if file.stat().st_mtime < cutoff_ts:
                    continue

            yield file


def score_sessions(
    project_filter: str | None = None,
    since_days: int | None = None,
    interactive_only: bool = True,
    min_score: float | None = None,
    limit: int = 20,
    verbose: bool = False,
) -> list[SessionScore]:
    """Score and rank sessions, returning top N by signal density.

    Args:
        project_filter: Substring match on project path.
        since_days: Only sessions modified in last N days.
        interactive_only: If True, only return interactive sessions.
        min_score: Only return sessions scoring >= this (0-100, pre-normalization).
        limit: Maximum number of sessions to return.
        verbose: Print progress to stderr.

    Returns:
        Sessions sorted by score descending (normalized to 0-100).
    """
    sessions: list[SessionScore] = []
    total_scanned = 0
    total_skipped = 0

    for path in walk_session_files(project_filter=project_filter, since_days=since_days):
        total_scanned += 1
        if verbose:
            print(f"  Scoring {path.name}...", file=sys.stderr, end="\r")

        scored = score_session_file(path)
        if scored is None:
            total_skipped += 1
            continue

        if interactive_only and not scored.is_interactive:
            continue

        sessions.append(scored)

    if verbose:
        print(f"  Scanned {total_scanned} files, scored {len(sessions)}   ", file=sys.stderr)

    # Normalize scores to 0-100
    sessions = _normalize_scores(sessions)

    # Apply min_score filter (post-normalization)
    if min_score is not None:
        sessions = [s for s in sessions if s.score >= min_score]

    # Sort by score descending
    sessions.sort(key=lambda s: (-s.score, s.last_activity or ""))

    return sessions[:limit]


def _format_project(path: str) -> str:
    """Format project path for display (last 2 components)."""
    parts = [p for p in path.split("/") if p]
    if len(parts) >= 2:
        return "/".join(parts[-2:])
    return path


def _format_date(iso: str | None) -> str:
    """Format ISO timestamp to short date string."""
    if not iso:
        return "unknown"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d")
    except (ValueError, OverflowError):
        return iso[:10] if len(iso) >= 10 else iso


def print_table(sessions: list[SessionScore]) -> None:
    """Print sessions as a human-readable table."""
    if not sessions:
        print("No sessions found.")
        return

    # Header
    print(f"{'SCORE':>5}  {'SESSION_ID':<36}  {'TURNS':>5}  {'EDITS':>5}  {'DATE':<10}  {'PROJECT':<24}  SUMMARY")
    print("-" * 110)

    for s in sessions:
        score_str = f"{s.score:>5.1f}"
        session_id = s.session_id[:36]
        human_str = f"{s.human_text_turns}hu"
        edit_str = f"{s.edit_tool_count}ed"
        date_str = _format_date(s.last_activity)
        project_str = _format_project(s.project_path)[:24]
        summary = s.first_human_message.replace("\n", " ")[:55]

        print(f"{score_str}  {session_id:<36}  {human_str:>5}  {edit_str:>5}  {date_str:<10}  {project_str:<24}  {summary}")


def print_json(sessions: list[SessionScore]) -> None:
    """Print sessions as JSON array."""
    output = []
    for s in sessions:
        d = {
            "session_id": s.session_id,
            "project_path": s.project_path,
            "file_path": s.file_path,
            "score": s.score,
            "is_interactive": s.is_interactive,
            "last_activity": s.last_activity,
            "metrics": {
                "total_events": s.total_events,
                "human_text_turns": s.human_text_turns,
                "human_text_chars": s.human_text_chars,
                "assistant_text_chars": s.assistant_text_chars,
                "thinking_chars": s.thinking_chars,
                "tool_call_count": s.tool_call_count,
                "unique_tools": s.unique_tools,
                "edit_tool_count": s.edit_tool_count,
                "session_duration_minutes": s.session_duration_minutes,
            },
            "first_human_message": s.first_human_message,
        }
        output.append(d)
    print(json.dumps(output, indent=2))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="shuttle score-sessions",
        description="Score and rank CC sessions by signal density",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Signal density measures how much meaningful human+Claude dialogue
happened per unit of session activity. Interactive sessions (human
actively engaged) are ranked highest.

Examples:
  shuttle score-sessions                          # top 20 interactive sessions
  shuttle score-sessions -p speakbot              # filter to speakbot project
  shuttle score-sessions -s 7                     # last 7 days only
  shuttle score-sessions -n 50 --json             # top 50 as JSON
  shuttle score-sessions --include-automated      # include automated sessions
  shuttle score-sessions --min-score 50           # only high-scoring sessions
""",
    )

    parser.add_argument(
        "--project", "-p",
        metavar="PROJECT",
        help="Filter by project path (substring match)",
    )
    parser.add_argument(
        "--since", "-s",
        type=int,
        metavar="DAYS",
        help="Only sessions from the last N days",
    )
    parser.add_argument(
        "--limit", "-n",
        type=int,
        default=20,
        metavar="N",
        help="Max sessions to show (default: 20)",
    )
    parser.add_argument(
        "--all", "-a",
        action="store_true",
        help="All projects (default when --project not set)",
    )
    parser.add_argument(
        "--json", "-j",
        action="store_true",
        help="Output as JSON",
    )
    parser.add_argument(
        "--min-score",
        type=float,
        metavar="SCORE",
        help="Only show sessions scoring >= SCORE (0-100)",
    )
    parser.add_argument(
        "--include-automated",
        action="store_true",
        help="Include automated sessions (default: interactive only)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show progress while scanning",
    )

    args = parser.parse_args(argv)

    if args.verbose:
        print(f"Scanning sessions...", file=sys.stderr)

    sessions = score_sessions(
        project_filter=args.project,
        since_days=args.since,
        interactive_only=not args.include_automated,
        min_score=args.min_score,
        limit=args.limit,
        verbose=args.verbose,
    )

    if args.json:
        print_json(sessions)
    else:
        print_table(sessions)
        if sessions:
            print(f"\n{len(sessions)} session(s) shown")


if __name__ == "__main__":
    main()
