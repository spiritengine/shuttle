"""CLI interface for shuttle index."""

import argparse
import json
import sys
from pathlib import Path

from .builder import build_index, update_index
from .query import get_stats, list_sessions, search_sessions


def cmd_build(args):
    """Build or rebuild index."""
    build_index(verify=args.verify, verbose=args.verbose)


def cmd_update(args):
    """Update index incrementally."""
    update_index(verify=args.verify, verbose=args.verbose)


def cmd_list(args):
    """List sessions."""
    sessions = list(list_sessions(
        limit=args.limit,
        since_days=args.since,
        project=args.project,
        status=args.status,
    ))

    if not sessions:
        print("No sessions found")
        return

    if args.json:
        output = []
        for s in sessions:
            output.append({
                "session_id": s.session_id,
                "project_path": s.project_path,
                "first_message": s.first_message,
                "last_activity": s.last_activity,
                "message_count": s.message_count,
                "total_tokens": s.total_tokens,
                "status": s.status,
            })
        print(json.dumps(output, indent=2))
    else:
        # Plain text output
        for s in sessions:
            # Truncate message
            msg = s.first_message or ""
            if len(msg) > 60:
                msg = msg[:60] + "..."

            # Format: session_id project_path message date
            print(f"{s.session_id}  {s.project_path}  {msg}  {s.last_activity or 'unknown'}")


def cmd_search(args):
    """Search sessions."""
    if not args.query:
        print("Error: search query required")
        sys.exit(1)

    sessions = list(search_sessions(
        query=args.query,
        limit=args.limit,
        project=args.project,
    ))

    if not sessions:
        print(f"No sessions found matching: {args.query}")
        return

    if args.json:
        output = []
        for s in sessions:
            output.append({
                "session_id": s.session_id,
                "project_path": s.project_path,
                "first_message": s.first_message,
                "last_activity": s.last_activity,
                "message_count": s.message_count,
                "total_tokens": s.total_tokens,
            })
        print(json.dumps(output, indent=2))
    else:
        # Plain text
        for s in sessions:
            msg = s.first_message or ""
            if len(msg) > 60:
                msg = msg[:60] + "..."
            print(f"{s.session_id}  {s.project_path}  {msg}")

        print(f"\nUse: shuttle resume <session-id>")


def cmd_stats(args):
    """Show statistics."""
    stats = get_stats(project=args.project)

    if args.json:
        print(json.dumps({
            "total_sessions": stats.total_sessions,
            "total_tokens": stats.total_tokens,
            "active_sessions": stats.active_sessions,
            "complete_sessions": stats.complete_sessions,
            "projects": [{"path": p, "count": c} for p, c in stats.projects],
            "largest_sessions": [
                {"session_id": sid, "tokens": t, "messages": m}
                for sid, t, m in stats.largest_sessions
            ],
        }, indent=2))
    else:
        print(f"Total sessions: {stats.total_sessions:,}")
        print(f"Total tokens: {stats.total_tokens:,}")
        print(f"Active: {stats.active_sessions}, Complete: {stats.complete_sessions}")
        print()
        print("Top projects:")
        for path, count in stats.projects[:5]:
            # Truncate path
            display_path = path if len(path) <= 40 else "..." + path[-37:]
            print(f"  {display_path:40} {count:4} sessions")
        print()
        print("Largest sessions:")
        for sid, tokens, msgs in stats.largest_sessions[:5]:
            print(f"  {sid}  {tokens:8,} tokens  {msgs:4} messages")


def main():
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="shuttle-index",
        description="Shuttle session indexing",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Build command
    build_parser = subparsers.add_parser("build", help="Build full index")
    build_parser.add_argument("--verify", action="store_true",
                             help="Compute content hashes (slow)")
    build_parser.add_argument("-v", "--verbose", action="store_true",
                             help="Show progress")
    build_parser.set_defaults(func=cmd_build)

    # Update command
    update_parser = subparsers.add_parser("update", help="Update index incrementally")
    update_parser.add_argument("--verify", action="store_true",
                              help="Use content hash verification (slow)")
    update_parser.add_argument("-v", "--verbose", action="store_true",
                              help="Show progress")
    update_parser.set_defaults(func=cmd_update)

    # List command
    list_parser = subparsers.add_parser("list", help="List sessions")
    list_parser.add_argument("-n", "--limit", type=int, default=10,
                            help="Number of sessions (default: 10)")
    list_parser.add_argument("--since", type=int,
                            help="Only sessions from last N days")
    list_parser.add_argument("--project", type=str,
                            help="Filter by project path")
    list_parser.add_argument("--status", choices=["active", "complete"],
                            help="Filter by status")
    list_parser.add_argument("--json", action="store_true",
                            help="Output JSON")
    list_parser.set_defaults(func=cmd_list)

    # Search command
    search_parser = subparsers.add_parser("search", help="Search sessions")
    search_parser.add_argument("query", nargs="?", help="Search query")
    search_parser.add_argument("-n", "--limit", type=int, default=20,
                              help="Number of results (default: 20)")
    search_parser.add_argument("--project", type=str,
                              help="Filter by project")
    search_parser.add_argument("--json", action="store_true",
                              help="Output JSON")
    search_parser.set_defaults(func=cmd_search)

    # Stats command
    stats_parser = subparsers.add_parser("stats", help="Show statistics")
    stats_parser.add_argument("--project", type=str,
                             help="Filter by project")
    stats_parser.add_argument("--json", action="store_true",
                             help="Output JSON")
    stats_parser.set_defaults(func=cmd_stats)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
