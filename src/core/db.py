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
    - raw_data (JSON) - Store minimized API responses (large arrays removed)
    - parent_uri (TEXT) - Extracted parent URI for tree traversal
    - foundation_id (TEXT) - Extracted foundation entity ID
    - chapter_info (JSON) - Minimal classification metadata
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
            raw_data TEXT NOT NULL DEFAULT '{}',
            parent_uri TEXT DEFAULT '',
            foundation_id TEXT DEFAULT '',
            chapter_info TEXT DEFAULT '{}'
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
    
    # Create indexes for new optimized columns
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_parent_uri ON icd_nodes_state(parent_uri)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_foundation_id ON icd_nodes_state(foundation_id)
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


def insert_pending_nodes_bulk_ignore(
    conn: sqlite3.Connection,
    uris: list[str],
    status: str = "PENDING",
) -> int:
    """Bulk insert nodes with IGNORE (skip if exists). Reduces disk I/O significantly.
    
    Returns the number of rows actually inserted (excluding duplicates).
    """
    cursor = conn.cursor()
    # Prepare data for executemany
    data = [(uri, status) for uri in uris]
    cursor.executemany("""
        INSERT OR IGNORE INTO icd_nodes_state (uri, status)
        VALUES (?, ?)
    """, data)
    conn.commit()
    # Return number of rows affected (inserted, not ignored)
    return cursor.rowcount


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
    parent_uri: str = "",
    foundation_id: str = "",
    chapter_info: dict[str, Any] | None = None,
) -> None:
    """Update node data fields."""
    if raw_data is None:
        raw_data = {}
    if chapter_info is None:
        chapter_info = {}
    cursor = conn.cursor()
    if status:
        cursor.execute("""
            UPDATE icd_nodes_state 
            SET icd_code = ?, title = ?, description = ?, raw_data = ?, status = ?,
                parent_uri = ?, foundation_id = ?, chapter_info = ?
            WHERE uri = ?
        """, (icd_code, title, description, json.dumps(raw_data), status, parent_uri, foundation_id, json.dumps(chapter_info), uri))
    else:
        cursor.execute("""
            UPDATE icd_nodes_state 
            SET icd_code = ?, title = ?, description = ?, raw_data = ?,
                parent_uri = ?, foundation_id = ?, chapter_info = ?
            WHERE uri = ?
        """, (icd_code, title, description, json.dumps(raw_data), parent_uri, foundation_id, json.dumps(chapter_info), uri))
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


def has_pending_nodes(conn: sqlite3.Connection) -> bool:
    """Check if there are any PENDING nodes in the queue."""
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM icd_nodes_state WHERE status = 'PENDING' LIMIT 1")
    return cursor.fetchone() is not None


def detect_stuck_state(conn: sqlite3.Connection) -> bool:
    """Detect 'stuck' or 'dead' database state.
    
    A stuck state occurs when:
    - Database is NOT empty (has at least one record)
    - But has NO PENDING nodes to process
    
    This indicates the sync process completed without actually syncing data,
    or the queue initialization was skipped due to non-empty DB check.
    
    Returns True if stuck state is detected.
    """
    cursor = conn.cursor()
    
    # Check total count
    cursor.execute("SELECT COUNT(*) FROM icd_nodes_state")
    total_count = cursor.fetchone()[0]
    
    if total_count == 0:
        return False  # Empty DB is not stuck, it needs seeding
    
    # Check for PENDING nodes
    cursor.execute("SELECT COUNT(*) FROM icd_nodes_state WHERE status = 'PENDING'")
    pending_count = cursor.fetchone()[0]
    
    # Stuck if: has records but no pending nodes
    return total_count > 0 and pending_count == 0


def recover_from_stuck_state(conn: sqlite3.Connection) -> int:
    """Recover from stuck state by re-seeding the queue.
    
    If any BASE_DONE node has 'child', 'release' or 'latestRelease' data, extract those URIs
    and add them as PENDING. Otherwise, mark existing nodes as PENDING again.
    
    Returns the number of nodes added to the queue.
    """
    cursor = conn.cursor()
    
    # Find ALL BASE_DONE nodes that might have child/release info
    cursor.execute("""
        SELECT uri, raw_data FROM icd_nodes_state 
        WHERE status = 'BASE_DONE' AND raw_data != '{}'
    """)
    rows = cursor.fetchall()
    
    inserted_count = 0
    
    for row in rows:
        import json
        node_uri = row[0]
        raw_data = json.loads(row[1])
        
        if not raw_data or not isinstance(raw_data, dict):
            continue
        
        # Extract child URIs using the same logic as in who_client.py
        child_uris = set()
        
        # Process 'child' field
        children = raw_data.get("child", [])
        
        # Handle case where children might be a dict (language-keyed or @list structure)
        if isinstance(children, dict):
            # Try JSON-LD @list structure first
            if '@list' in children:
                children = children['@list']
            else:
                # Try to get English version first, otherwise take first available
                children = children.get('en', []) or children.get('en-US', []) or next(iter(children.values()), [])
        
        if isinstance(children, list):
            for child in children:
                if isinstance(child, str):
                    # Direct URI string
                    uri = child.replace("http://", "https://")
                    child_uris.add(uri)
                elif isinstance(child, dict):
                    # Object with @id field (JSON-LD reference)
                    if "@id" in child:
                        uri = child["@id"].replace("http://", "https://")
                        child_uris.add(uri)
                    # Check for nested structures where @id might be in a sub-object
                    elif "target" in child and isinstance(child["target"], dict):
                        target = child["target"]
                        if "@id" in target:
                            uri = target["@id"].replace("http://", "https://")
                            child_uris.add(uri)
                    # Check for embedded entity with @id at top level of nested dict
                    elif "@graph" in child:
                        graph = child["@graph"]
                        if isinstance(graph, list) and len(graph) > 0:
                            for item in graph:
                                if isinstance(item, dict) and "@id" in item:
                                    uri = item["@id"].replace("http://", "https://")
                                    child_uris.add(uri)
        
        # Process 'release' field (array of release version URIs)
        releases = raw_data.get("release", [])
        if isinstance(releases, list):
            for release in releases:
                if isinstance(release, str):
                    uri = release.replace("http://", "https://")
                    child_uris.add(uri)
                elif isinstance(release, dict) and "@id" in release:
                    uri = release["@id"].replace("http://", "https://")
                    child_uris.add(uri)
        
        # Process 'latestRelease' field (single URI)
        latest_release = raw_data.get("latestRelease", "")
        if isinstance(latest_release, str) and latest_release:
            uri = latest_release.replace("http://", "https://")
            child_uris.add(uri)
        elif isinstance(latest_release, dict) and "@id" in latest_release:
            uri = latest_release["@id"].replace("http://", "https://")
            child_uris.add(uri)
        
        # Insert all discovered URIs as PENDING
        if child_uris:
            for uri in child_uris:
                cursor.execute("""
                    INSERT OR IGNORE INTO icd_nodes_state (uri, status)
                    VALUES (?, 'PENDING')
                """, (uri,))
                if cursor.rowcount > 0:
                    inserted_count += 1
    
    # If we found and inserted child URIs, commit and return
    if inserted_count > 0:
        conn.commit()
        return inserted_count
    
    # Fallback: re-insert the first node as PENDING if no child URIs found
    cursor.execute("""
        UPDATE OR IGNORE icd_nodes_state SET status = 'PENDING'
        WHERE uri = (SELECT uri FROM icd_nodes_state LIMIT 1)
    """)
    conn.commit()
    
    return cursor.rowcount
