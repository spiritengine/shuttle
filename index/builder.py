"""Build and update session index."""

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Iterator

from knurl import hash as knurl_hash

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from parser import Session

from .schema import get_index_path, init_db


def walk_session_files() -> Iterator[Path]:
    """Walk all CC session files.

    Yields:
        Paths to session JSONL files (excluding agent subfiles)
    """
    cc_projects = Path.home() / ".claude" / "projects"
    if not cc_projects.exists():
        return

    for project_dir in cc_projects.iterdir():
        if not project_dir.is_dir():
            continue

        for file in project_dir.glob("*.jsonl"):
            # Skip agent files
            if file.name.startswith("agent-"):
                continue
            yield file


def decode_project_path(encoded: str) -> str:
    """Decode project path from directory name.

    Args:
        encoded: Encoded path like '-home-patrick-projects-speakbot'

    Returns:
        Decoded path like '/home/patrick/projects/speakbot'
    """
    return "/" + encoded.lstrip("-").replace("-", "/")


def detect_status(session: Session, file_path: Path) -> str:
    """Detect session status.

    Args:
        session: Parsed session
        file_path: Path to session file

    Returns:
        'active' or 'complete'
    """
    # Active if modified in last 24 hours
    mtime = file_path.stat().st_mtime
    age_hours = (datetime.now().timestamp() - mtime) / 3600

    if age_hours < 24:
        return "active"
    return "complete"


def extract_first_message(session: Session) -> str:
    """Extract first user message for summary.

    Args:
        session: Parsed session

    Returns:
        First user message text, truncated to 200 chars
    """
    for event in session.events:
        if event.type == "user" and event.message:
            content = event.message.content
            if content:
                # Get text from first text block
                for block in content:
                    if hasattr(block, 'text'):
                        return block.text[:200]
    return ""


def index_session(file_path: Path, conn: sqlite3.Connection, verify: bool = False):
    """Index a single session file.

    Args:
        file_path: Path to session JSONL file
        conn: Database connection
        verify: If True, compute content hash
    """
    session_id = file_path.stem
    project_path = decode_project_path(file_path.parent.name)
    file_mtime = file_path.stat().st_mtime

    try:
        # Parse session
        session = Session.from_path(file_path)

        # Extract metadata
        started = None
        last_activity = None
        if session.events:
            first_event = session.events[0]
            if hasattr(first_event, 'timestamp'):
                started = first_event.timestamp

            last_event = session.events[-1]
            if hasattr(last_event, 'timestamp'):
                last_activity = last_event.timestamp

        message_count = len([e for e in session.events if e.type in ("user", "assistant")])
        total_tokens = session.total_tokens()
        status = detect_status(session, file_path)
        first_message = extract_first_message(session)

        # Optional content hash
        content_hash = None
        if verify:
            content = file_path.read_text()
            content_hash = knurl_hash.compute(content)

        # Insert/update session
        indexed_at = datetime.now().isoformat()
        conn.execute("""
            INSERT OR REPLACE INTO sessions (
                session_id, project_path, file_path,
                started, last_activity, message_count, total_tokens,
                status, first_message, file_mtime, content_hash, indexed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            session_id, project_path, str(file_path),
            started, last_activity, message_count, total_tokens,
            status, first_message, file_mtime, content_hash, indexed_at
        ))

        # Index content for FTS
        # Collect all text content
        content_parts = []
        for event in session.events:
            if event.message:
                for block in event.message.content:
                    if hasattr(block, 'text') and block.text:
                        content_parts.append(block.text)

        if content_parts:
            full_content = " ".join(content_parts)
            conn.execute("""
                INSERT OR REPLACE INTO sessions_fts (session_id, content)
                VALUES (?, ?)
            """, (session_id, full_content))

    except Exception as e:
        print(f"Error indexing {file_path}: {e}")


def build_index(verify: bool = False, verbose: bool = False):
    """Build complete index from scratch.

    Args:
        verify: If True, compute content hashes (slow)
        verbose: If True, show progress
    """
    db_path = get_index_path()
    conn = init_db(db_path)

    # Clear existing data
    conn.execute("DELETE FROM sessions")
    conn.execute("DELETE FROM sessions_fts")

    # Index all sessions
    total = 0
    for file_path in walk_session_files():
        if verbose:
            print(f"Indexing {file_path.name}...")
        index_session(file_path, conn, verify=verify)
        total += 1

        if total % 100 == 0:
            conn.commit()

    conn.commit()
    conn.close()

    print(f"Indexed {total} sessions")
    if verify:
        print("  with content hash verification")


def update_index(verify: bool = False, verbose: bool = False):
    """Incrementally update index.

    Args:
        verify: If True, use content hash instead of mtime (slow but robust)
        verbose: If True, show progress
    """
    db_path = get_index_path()

    if not db_path.exists():
        print("No index found. Run 'shuttle index' to build.")
        return

    conn = init_db(db_path)

    updated = 0
    skipped = 0

    for file_path in walk_session_files():
        session_id = file_path.stem

        # Get indexed mtime/hash
        cursor = conn.execute(
            "SELECT file_mtime, content_hash FROM sessions WHERE session_id = ?",
            (session_id,)
        )
        row = cursor.fetchone()

        needs_update = False

        if row is None:
            # New session
            needs_update = True
        elif verify and row["content_hash"]:
            # Hash-based verification
            current_hash = knurl_hash.compute(file_path.read_text())
            if current_hash != row["content_hash"]:
                needs_update = True
        else:
            # mtime-based (fast path)
            current_mtime = file_path.stat().st_mtime
            indexed_mtime = row["file_mtime"]
            if current_mtime > indexed_mtime:
                needs_update = True

        if needs_update:
            if verbose:
                print(f"Updating {file_path.name}...")
            index_session(file_path, conn, verify=verify)
            updated += 1
        else:
            skipped += 1

        if (updated + skipped) % 100 == 0:
            conn.commit()

    conn.commit()
    conn.close()

    print(f"Updated {updated} sessions, skipped {skipped}")
    if verify:
        print("  with content hash verification")
