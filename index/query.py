"""Query session index."""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator

from .schema import get_index_path


@dataclass
class SessionRecord:
    """Single session record from index."""
    session_id: str
    project_path: str
    file_path: str
    started: str | None
    last_activity: str | None
    message_count: int
    total_tokens: int
    status: str
    first_message: str
    file_mtime: float
    indexed_at: str


def list_sessions(
    limit: int = 10,
    since_days: int | None = None,
    project: str | None = None,
    status: str | None = None,
) -> Iterator[SessionRecord]:
    """List sessions from index.

    Args:
        limit: Maximum number of sessions to return
        since_days: Only sessions from last N days
        project: Filter by project path (substring match)
        status: Filter by status ('active' or 'complete')

    Yields:
        SessionRecord objects, most recent first
    """
    db_path = get_index_path()
    if not db_path.exists():
        return

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Build query
    where_clauses = []
    params = []

    if since_days:
        cutoff = datetime.now() - timedelta(days=since_days)
        where_clauses.append("last_activity >= ?")
        params.append(cutoff.isoformat())

    if project:
        where_clauses.append("project_path LIKE ?")
        params.append(f"%{project}%")

    if status:
        where_clauses.append("status = ?")
        params.append(status)

    where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"

    cursor = conn.execute(f"""
        SELECT
            session_id, project_path, file_path,
            started, last_activity, message_count, total_tokens,
            status, first_message, file_mtime, indexed_at
        FROM sessions
        WHERE {where_sql}
        ORDER BY last_activity DESC
        LIMIT ?
    """, params + [limit])

    for row in cursor:
        yield SessionRecord(**dict(row))

    conn.close()


def search_sessions(
    query: str,
    limit: int = 20,
    project: str | None = None,
) -> Iterator[SessionRecord]:
    """Search sessions by content.

    Args:
        query: Search query (FTS5 syntax)
        limit: Maximum results
        project: Filter by project

    Yields:
        SessionRecord objects matching query
    """
    db_path = get_index_path()
    if not db_path.exists():
        return

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Build query
    where_clauses = ["sessions_fts.session_id = sessions.session_id"]
    params = [query]

    if project:
        where_clauses.append("sessions.project_path LIKE ?")
        params.append(f"%{project}%")

    where_sql = " AND ".join(where_clauses)

    cursor = conn.execute(f"""
        SELECT
            sessions.session_id, project_path, file_path,
            started, last_activity, message_count, total_tokens,
            status, first_message, file_mtime, indexed_at
        FROM sessions_fts
        JOIN sessions ON {where_sql}
        WHERE sessions_fts MATCH ?
        ORDER BY sessions.last_activity DESC
        LIMIT ?
    """, params + [limit])

    for row in cursor:
        yield SessionRecord(**dict(row))

    conn.close()


@dataclass
class IndexStats:
    """Statistics about the session index."""
    total_sessions: int
    total_tokens: int
    active_sessions: int
    complete_sessions: int
    projects: list[tuple[str, int]]  # (project_path, count)
    largest_sessions: list[tuple[str, int, int]]  # (session_id, tokens, messages)


def get_stats(project: str | None = None) -> IndexStats:
    """Get index statistics.

    Args:
        project: Filter by project (substring match)

    Returns:
        IndexStats object
    """
    db_path = get_index_path()
    if not db_path.exists():
        return IndexStats(0, 0, 0, 0, [], [])

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Build where clause
    where_sql = "1=1"
    params = []
    if project:
        where_sql = "project_path LIKE ?"
        params = [f"%{project}%"]

    # Total sessions and tokens
    cursor = conn.execute(f"""
        SELECT
            COUNT(*) as total,
            SUM(total_tokens) as tokens,
            SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) as active,
            SUM(CASE WHEN status = 'complete' THEN 1 ELSE 0 END) as complete
        FROM sessions
        WHERE {where_sql}
    """, params)
    row = cursor.fetchone()
    total = row["total"] or 0
    tokens = row["tokens"] or 0
    active = row["active"] or 0
    complete = row["complete"] or 0

    # By project
    cursor = conn.execute(f"""
        SELECT project_path, COUNT(*) as count
        FROM sessions
        WHERE {where_sql}
        GROUP BY project_path
        ORDER BY count DESC
        LIMIT 10
    """, params)
    projects = [(row["project_path"], row["count"]) for row in cursor]

    # Largest sessions
    cursor = conn.execute(f"""
        SELECT session_id, total_tokens, message_count
        FROM sessions
        WHERE {where_sql}
        ORDER BY total_tokens DESC
        LIMIT 10
    """, params)
    largest = [(row["session_id"], row["total_tokens"], row["message_count"])
               for row in cursor]

    conn.close()

    return IndexStats(
        total_sessions=total,
        total_tokens=tokens,
        active_sessions=active,
        complete_sessions=complete,
        projects=projects,
        largest_sessions=largest,
    )
