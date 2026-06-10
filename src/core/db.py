"""Core database module for SQLite state management."""

import json
import sqlite3
from pathlib import Path
from typing import Any


def get_db_path(data_dir: Path) -> Path:
    """Get the path to the SQLite database file."""
    db_dir = data_dir / "db"
    db_dir.mkdir(parents=True, exist_ok=True)
    return db_dir / "sync_state.db"


def init_db(db_path: Path) -> sqlite3.Connection:
    """Initialize SQLite database and create tables if they don't exist.
    
    Creates the icd_nodes_state table with schema for queue-based processing:
    - uri (TEXT, Primary Key) - Full WHO API URI for the node
    - icd_code (TEXT) - Extracted ICD code (e.g., '1B21.0')
    - title (TEXT)
    - description (TEXT)
    - status (TEXT) - Enum: 'PENDING', 'BASE_DONE', 'ENRICHED', 'VECTORIZED'
    - raw_data (JSON) - Store API responses here
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS icd_nodes_state (
            uri TEXT PRIMARY KEY,
            icd_code TEXT DEFAULT '',
            title TEXT DEFAULT '',
            description TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'PENDING',
            raw_data TEXT NOT NULL DEFAULT '{}'
        )
    """)
    
    # Create index on status for efficient filtering
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_status ON icd_nodes_state(status)
    """)
    
    # Create index on icd_code for lookups
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_icd_code ON icd_nodes_state(icd_code)
    """)
    
    conn.commit()
    return conn


def insert_or_update_node(
    conn: sqlite3.Connection,
    uri: str,
    icd_code: str = "",
    title: str = "",
    description: str = "",
    raw_data: dict[str, Any] | None = None,
    status: str = "PENDING",
) -> None:
    """Insert or update a node in the database."""
    if raw_data is None:
        raw_data = {}
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO icd_nodes_state (uri, icd_code, title, description, status, raw_data)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (uri, icd_code, title, description, status, json.dumps(raw_data)))
    conn.commit()


def insert_pending_node_ignore(
    conn: sqlite3.Connection,
    uri: str,
    status: str = "PENDING",
) -> None:
    """Insert a node with IGNORE (skip if exists). Used for queue-based tree traversal."""
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR IGNORE INTO icd_nodes_state (uri, status)
        VALUES (?, ?)
    """, (uri, status))
    conn.commit()


def get_nodes_by_status(conn: sqlite3.Connection, status: str, limit: int = 500) -> list[sqlite3.Row]:
    """Get nodes with a specific status, limited for batch processing."""
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM icd_nodes_state WHERE status = ? LIMIT ?", (status, limit))
    return cursor.fetchall()


def update_node_status(conn: sqlite3.Connection, uri: str, status: str) -> None:
    """Update the status of a node."""
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE icd_nodes_state SET status = ? WHERE uri = ?
    """, (status, uri))
    conn.commit()


def update_node_data(
    conn: sqlite3.Connection,
    uri: str,
    icd_code: str = "",
    title: str = "",
    description: str = "",
    raw_data: dict[str, Any] | None = None,
    status: str | None = None,
) -> None:
    """Update node data fields."""
    if raw_data is None:
        raw_data = {}
    cursor = conn.cursor()
    if status:
        cursor.execute("""
            UPDATE icd_nodes_state 
            SET icd_code = ?, title = ?, description = ?, raw_data = ?, status = ?
            WHERE uri = ?
        """, (icd_code, title, description, json.dumps(raw_data), status, uri))
    else:
        cursor.execute("""
            UPDATE icd_nodes_state 
            SET icd_code = ?, title = ?, description = ?, raw_data = ?
            WHERE uri = ?
        """, (icd_code, title, description, json.dumps(raw_data), uri))
    conn.commit()


def get_node_by_uri(conn: sqlite3.Connection, uri: str) -> sqlite3.Row | None:
    """Get a node by its URI."""
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM icd_nodes_state WHERE uri = ?", (uri,))
    return cursor.fetchone()


def get_node_by_code(conn: sqlite3.Connection, icd_code: str) -> sqlite3.Row | None:
    """Get a node by its ICD code."""
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM icd_nodes_state WHERE icd_code = ?", (icd_code,))
    return cursor.fetchone()


def get_all_nodes(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Get all nodes from the database."""
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM icd_nodes_state")
    return cursor.fetchall()


def count_nodes_by_status(conn: sqlite3.Connection, status: str) -> int:
    """Count nodes with a specific status."""
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM icd_nodes_state WHERE status = ?", (status,))
    return cursor.fetchone()[0]


def is_db_empty(conn: sqlite3.Connection) -> bool:
    """Check if the icd_nodes_state table is empty."""
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM icd_nodes_state")
    return cursor.fetchone()[0] == 0
