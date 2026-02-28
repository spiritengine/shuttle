#!/usr/bin/env python3
"""Extract (context, response) training pairs from CC session JSONL files.

Each pair captures a human's message in context — the preceding conversation
(user + assistant turns, text only) is the context, the human's text is the
response. Useful for training models to predict human follow-ups.

Skips:
  - Pure tool-result user messages (no human text)
  - First human message in a session (no context yet, unless --min-context 0)
  - Empty/whitespace-only messages

Usage (standalone):
    python3 parser/extract_pairs.py ~/.claude/projects/-home-patrick-projects-shuttle/abc.jsonl
    python3 parser/extract_pairs.py --all
    python3 parser/extract_pairs.py --project shuttle --output pairs.jsonl
    python3 parser/extract_pairs.py --all --stats

Output (JSONL, one pair per line):
    {"context": [{"role": "user", "content": "..."}, ...],
     "response": "human text",
     "session_id": "...", "timestamp": "...", "project": "..."}
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .models import Event
from .parse import parse


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Pair:
    """A single (context, response) training pair."""
    context: list[dict]   # Messages seen before this response
    response: str         # Human's text
    session_id: str
    timestamp: str        # ISO timestamp of the response event
    project: str          # Basename of cwd


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------

# CC injects terminal activity as text blocks in user messages.
# These are not human-typed conversational text.
_INJECTION_PREFIXES = (
    "<local-command-caveat>",
    "<bash-input>",
    "<bash-stdout>",
    "<bash-stderr>",
    "<system-reminder>",
    "<function_calls>",
)


def _is_injection(text: str) -> bool:
    """Return True if text is a CC-injected system block, not human input."""
    t = text.strip()
    return any(t.startswith(prefix) for prefix in _INJECTION_PREFIXES)


def _human_text(event: Event) -> str | None:
    """Extract plain human text from a user event.

    Returns None if the message is purely tool results or system injections.
    """
    if not event.message or event.message.role != "user":
        return None
    texts = [
        b.text for b in event.message.content
        if b.is_text and b.text and not _is_injection(b.text)
    ]
    text = "\n".join(texts).strip()
    return text or None


def _assistant_text(event: Event) -> str | None:
    """Extract plain text from an assistant event (skip thinking/tool blocks)."""
    if not event.message or event.message.role != "assistant":
        return None
    texts = [
        b.text for b in event.message.content
        if b.is_text and b.text
    ]
    text = "\n".join(texts).strip()
    return text or None


# ---------------------------------------------------------------------------
# Core extraction
# ---------------------------------------------------------------------------

def extract_pairs(path: Path | str, min_context: int = 1) -> Iterator[Pair]:
    """Yield (context, response) pairs from a single session file.

    Args:
        path: Path to .jsonl session file.
        min_context: Minimum number of messages in context before emitting.
            Default 1 means we skip the very first human message (no prior context).
            Set 0 to include it (with empty context).
    """
    path = Path(path)

    # Collect metadata from first events
    session_id = ""
    project = ""

    # We need to scan once for metadata, then iterate for pairs.
    # Use a single pass: grab metadata lazily as we encounter it.
    context: list[dict] = []

    for event in parse(path):
        # Grab metadata from first event that has it
        if not session_id and event.session_id:
            session_id = event.session_id
        if not project and event.cwd:
            project = Path(event.cwd).name

        if not event.is_conversation or not event.message:
            continue

        role = event.message.role

        if role == "user":
            text = _human_text(event)
            if text is not None:
                if len(context) >= min_context:
                    yield Pair(
                        context=list(context),
                        response=text,
                        session_id=session_id,
                        timestamp=event.timestamp,
                        project=project,
                    )
                # Add this human turn to context for future pairs
                context.append({"role": "user", "content": text})

        elif role == "assistant":
            text = _assistant_text(event)
            if text is not None:
                # Merge consecutive assistant turns (streaming produces multiple events)
                if context and context[-1]["role"] == "assistant":
                    context[-1]["content"] += "\n" + text
                else:
                    context.append({"role": "assistant", "content": text})


# ---------------------------------------------------------------------------
# Multi-session extraction
# ---------------------------------------------------------------------------

def _is_recent(path: Path, since_days: int | None) -> bool:
    """Return True if file was modified within since_days days."""
    if since_days is None:
        return True
    mtime = path.stat().st_mtime
    age_days = (datetime.now(timezone.utc).timestamp() - mtime) / 86400
    return age_days <= since_days


def find_session_files(
    cc_projects: Path,
    project: str | None = None,
    since_days: int | None = None,
) -> list[Path]:
    """Find session JSONL files under ~/.claude/projects/.

    Args:
        cc_projects: Root directory (~/.claude/projects/).
        project: If set, filter to directories containing this project name.
        since_days: If set, only files modified within N days.
    """
    files = []
    for f in cc_projects.rglob("*.jsonl"):
        # Skip agent session files
        if f.name.startswith("agent-"):
            continue
        # Filter by project name (encoded path)
        if project:
            encoded = f.parent.name
            # CC encodes paths as -home-user-projects-name
            # Accept substring match against the directory name
            if project not in encoded:
                continue
        if not _is_recent(f, since_days):
            continue
        files.append(f)
    return sorted(files, key=lambda f: f.stat().st_mtime, reverse=True)


def extract_pairs_from_files(
    files: list[Path],
    min_context: int = 1,
    verbose: bool = False,
) -> Iterator[Pair]:
    """Yield pairs from a list of session files."""
    for f in files:
        if verbose:
            print(f"  {f}", file=sys.stderr)
        try:
            yield from extract_pairs(f, min_context=min_context)
        except Exception as e:
            print(f"Warning: skipping {f}: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def compute_stats(pairs: list[Pair]) -> dict:
    """Compute summary statistics over a list of pairs."""
    if not pairs:
        return {"total_pairs": 0}

    projects: dict[str, int] = {}
    sessions: set[str] = set()
    response_lengths = [len(p.response) for p in pairs]

    for p in pairs:
        projects[p.project] = projects.get(p.project, 0) + 1
        sessions.add(p.session_id)

    return {
        "total_pairs": len(pairs),
        "sessions": len(sessions),
        "projects": len(projects),
        "top_projects": sorted(projects.items(), key=lambda x: -x[1])[:10],
        "avg_response_len": sum(response_lengths) / len(response_lengths),
        "avg_context_turns": sum(len(p.context) for p in pairs) / len(pairs),
    }


def print_stats(pairs: list[Pair]) -> None:
    """Print human-readable statistics."""
    stats = compute_stats(pairs)
    if stats["total_pairs"] == 0:
        print("No pairs found.")
        return
    print(f"Pairs:    {stats['total_pairs']:,}")
    print(f"Sessions: {stats['sessions']:,}")
    print(f"Projects: {stats['projects']:,}")
    print(f"Avg response length: {stats['avg_response_len']:.0f} chars")
    print(f"Avg context turns:   {stats['avg_context_turns']:.1f}")
    print()
    print("Top projects:")
    for proj, count in stats["top_projects"]:
        print(f"  {proj:30} {count:5,}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="shuttle extract-pairs",
        description="Extract (context, response) training pairs from CC sessions.",
        epilog="""
Examples:
  shuttle extract-pairs session.jsonl
  shuttle extract-pairs --all --output pairs.jsonl
  shuttle extract-pairs --project shuttle --stats
  shuttle extract-pairs --all --since 30 --min-context 2
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "files", nargs="*", type=Path,
        help="Session JSONL file(s) to process.",
    )
    parser.add_argument(
        "--all", "-a", action="store_true",
        help="Walk all CC sessions (~/.claude/projects/).",
    )
    parser.add_argument(
        "--project", "-p", type=str,
        help="Filter sessions by project name (substring match on encoded path).",
    )
    parser.add_argument(
        "--since", "-s", type=int, metavar="DAYS",
        help="Only sessions modified within last N days.",
    )
    parser.add_argument(
        "--output", "-o", type=Path,
        help="Write output to FILE (default: stdout).",
    )
    parser.add_argument(
        "--min-context", type=int, default=1, metavar="N",
        help="Minimum context messages before emitting a pair (default: 1).",
    )
    parser.add_argument(
        "--stats", action="store_true",
        help="Print statistics instead of JSONL pairs.",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Show progress on stderr.",
    )

    args = parser.parse_args(argv)

    # Determine input files
    if args.files:
        files = args.files
        for f in files:
            if not f.exists():
                print(f"Error: file not found: {f}", file=sys.stderr)
                return 1
    elif args.all or args.project:
        cc_projects = Path.home() / ".claude" / "projects"
        if not cc_projects.exists():
            print(f"Error: CC projects directory not found: {cc_projects}", file=sys.stderr)
            return 1
        files = find_session_files(cc_projects, project=args.project, since_days=args.since)
        if not files:
            print("No session files found.", file=sys.stderr)
            return 1
        if args.verbose:
            print(f"Found {len(files)} session file(s).", file=sys.stderr)
    else:
        parser.print_help()
        return 0

    # Extract pairs
    if args.stats:
        # Collect all for stats
        pairs = list(extract_pairs_from_files(
            files, min_context=args.min_context, verbose=args.verbose
        ))
        print_stats(pairs)
        return 0

    # Stream JSONL output
    out = open(args.output, "w") if args.output else sys.stdout
    count = 0
    try:
        for pair in extract_pairs_from_files(
            files, min_context=args.min_context, verbose=args.verbose
        ):
            out.write(json.dumps(asdict(pair), ensure_ascii=False) + "\n")
            count += 1
    except BrokenPipeError:
        # Downstream consumer (head, less, etc.) closed the pipe — exit quietly
        sys.stderr.close()
        return 0
    finally:
        if args.output:
            out.close()

    if args.verbose or args.output:
        dest = str(args.output) if args.output else "stdout"
        print(f"Wrote {count:,} pairs to {dest}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
