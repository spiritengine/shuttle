"""SQLite schema for session index."""

import sqlite3
from pathlib import Path


INDEX_VERSION = 1


def get_index_path() -> Path:
    """Get path to index database."""
    shuttle_dir = Path.home() / ".shuttle"
    shuttle_dir.mkdir(exist_ok=True)
    return shuttle_dir / "index.db"


def init_db(db_path: Path | None = None) -> sqlite3.Connection:
    """Initialize database with schema.

    Args:
        db_path: Path to database file. If None, uses default location.

    Returns:
        Database connection
    """
    if db_path is None:
        db_path = get_index_path()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Create schema
    conn.executescript("""
        -- Metadata table for versioning
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        -- Sessions table
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            project_path TEXT NOT NULL,
            file_path TEXT NOT NULL,
            started TEXT,
            last_activity TEXT,
            message_count INTEGER,
            total_tokens INTEGER,
            status TEXT,
            first_message TEXT,
            file_mtime REAL NOT NULL,
            content_hash TEXT,
            indexed_at TEXT NOT NULL
        );

        -- Indexes for common queries
        CREATE INDEX IF NOT EXISTS idx_project
            ON sessions(project_path);
        CREATE INDEX IF NOT EXISTS idx_activity
            ON sessions(last_activity DESC);
        CREATE INDEX IF NOT EXISTS idx_status
            ON sessions(status);
        CREATE INDEX IF NOT EXISTS idx_mtime
            ON sessions(file_mtime);

        -- FTS5 virtual table for full-text search
        CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts USING fts5(
            session_id UNINDEXED,
            content,
            tokenize='porter unicode61'
        );
    """)

    # Store schema version
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
        ("schema_version", str(INDEX_VERSION))
    )

    conn.commit()
    return conn


def get_schema_version(conn: sqlite3.Connection) -> int:
    """Get current schema version from database."""
    cursor = conn.execute(
        "SELECT value FROM meta WHERE key = ?",
        ("schema_version",)
    )
    row = cursor.fetchone()
    if row:
        return int(row[0])
    return 0
