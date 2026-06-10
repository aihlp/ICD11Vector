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
    
    Creates the icd_nodes_state table with schema:
    - icd_code (TEXT, Primary Key)
    - title (TEXT)
    - description (TEXT)
    - status (TEXT) - Enum: 'PENDING', 'ENRICHED', 'VECTORIZED'
    - raw_data (JSON) - Store API responses here
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS icd_nodes_state (
            icd_code TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'PENDING',
            raw_data TEXT NOT NULL
        )
    """)
    
    # Create index on status for efficient filtering
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_status ON icd_nodes_state(status)
    """)
    
    conn.commit()
    return conn


def insert_or_update_node(
    conn: sqlite3.Connection,
    icd_code: str,
    title: str,
    description: str,
    raw_data: dict[str, Any],
    status: str = "PENDING",
) -> None:
    """Insert or update a node in the database."""
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO icd_nodes_state (icd_code, title, description, status, raw_data)
        VALUES (?, ?, ?, ?, ?)
    """, (icd_code, title, description, status, json.dumps(raw_data)))
    conn.commit()


def get_nodes_by_status(conn: sqlite3.Connection, status: str) -> list[sqlite3.Row]:
    """Get all nodes with a specific status."""
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM icd_nodes_state WHERE status = ?", (status,))
    return cursor.fetchall()


def update_node_status(conn: sqlite3.Connection, icd_code: str, status: str) -> None:
    """Update the status of a node."""
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE icd_nodes_state SET status = ? WHERE icd_code = ?
    """, (status, icd_code))
    conn.commit()


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
