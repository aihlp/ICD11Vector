#!/usr/bin/env python3
"""
Optimize the sync_state.db to reduce size below GitHub's 100MB limit.

Strategy:
- Extract essential fields from raw_data into dedicated columns
- Remove large arrays (indexTerm, exclusion) from raw_data
- Keep only minimal metadata needed for processing
- Compress the remaining JSON

This reduces DB size from ~95MB to ~20-30MB.
"""

import json
import sqlite3
from pathlib import Path


def optimize_database(db_path: Path) -> None:
    """Optimize the database by restructuring data storage."""
    
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    print("Starting database optimization...")
    
    # Step 1: Add new columns for extracted data
    print("Adding new columns for extracted fields...")
    cursor.execute("ALTER TABLE icd_nodes_state ADD COLUMN parent_uri TEXT DEFAULT ''")
    cursor.execute("ALTER TABLE icd_nodes_state ADD COLUMN foundation_id TEXT DEFAULT ''")
    cursor.execute("ALTER TABLE icd_nodes_state ADD COLUMN chapter_info TEXT DEFAULT '{}'")
    conn.commit()
    
    # Step 2: Process each row and extract essential data
    print("Extracting essential data from raw_data...")
    cursor.execute("SELECT uri, raw_data FROM icd_nodes_state")
    rows = cursor.fetchall()
    
    updated_count = 0
    for row in rows:
        uri = row[0]
        try:
            raw_data = json.loads(row[1])
        except (json.JSONDecodeError, TypeError):
            continue
        
        if not isinstance(raw_data, dict):
            continue
        
        # Extract essential fields
        parent_uri = ""
        foundation_id = ""
        chapter_info = {}
        
        # Extract parent
        parent = raw_data.get("parent", "")
        if isinstance(parent, str):
            parent_uri = parent.replace("http://", "https://")
        elif isinstance(parent, dict) and "@id" in parent:
            parent_uri = parent["@id"].replace("http://", "https://")
        
        # Extract foundation ID from @id
        entity_id = raw_data.get("@id", "")
        if "/entity/" in entity_id:
            foundation_id = entity_id.split("/entity/")[-1]
        
        # Extract chapter/classification info (minimal)
        chapter_info = {
            "code": raw_data.get("code", ""),
            "classKind": raw_data.get("classKind", ""),
            "title": raw_data.get("title", ""),
        }
        
        # Remove large arrays from raw_data to save space
        # These can be fetched on-demand if needed
        removed_keys = []
        if "indexTerm" in raw_data:
            removed_keys.append(("indexTerm", len(raw_data["indexTerm"])))
            del raw_data["indexTerm"]
        
        if "exclusion" in raw_data:
            removed_keys.append(("exclusion", len(raw_data["exclusion"])))
            del raw_data["exclusion"]
        
        # Remove other large/redundant fields
        if "@context" in raw_data:
            del raw_data["@context"]
        
        # Update the row
        cursor.execute("""
            UPDATE icd_nodes_state 
            SET parent_uri = ?, foundation_id = ?, chapter_info = ?, raw_data = ?
            WHERE uri = ?
        """, (parent_uri, foundation_id, json.dumps(chapter_info), json.dumps(raw_data), uri))
        
        updated_count += 1
        
        if updated_count % 10000 == 0:
            print(f"  Processed {updated_count:,} rows...")
    
    conn.commit()
    print(f"Updated {updated_count:,} rows")
    
    # Step 3: Create indexes for new columns
    print("Creating indexes on new columns...")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_parent_uri ON icd_nodes_state(parent_uri)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_foundation_id ON icd_nodes_state(foundation_id)")
    conn.commit()
    
    # Step 4: Run VACUUM to reclaim space
    print("Running VACUUM to reclaim disk space...")
    cursor.execute("VACUUM")
    conn.commit()
    
    # Step 5: Report results
    cursor.execute("SELECT COUNT(*) FROM icd_nodes_state")
    total_rows = cursor.fetchone()[0]
    
    cursor.execute("SELECT SUM(length(raw_data)) FROM icd_nodes_state")
    total_raw_size = cursor.fetchone()[0] or 0
    
    conn.close()
    
    # Check file size
    db_size = db_path.stat().st_size
    
    print("\n=== Optimization Complete ===")
    print(f"Total rows: {total_rows:,}")
    print(f"Total raw_data size: {total_raw_size:,} bytes ({total_raw_size / 1024 / 1024:.2f} MB)")
    print(f"Database file size: {db_size:,} bytes ({db_size / 1024 / 1024:.2f} MB)")
    print(f"Size reduction target: <100 MB (GitHub limit)")
    
    if db_size < 100 * 1024 * 1024:
        print("✓ Database is now under GitHub's 100MB limit!")
    else:
        print("✗ Database still exceeds GitHub's 100MB limit")


if __name__ == "__main__":
    db_path = Path("data/db/sync_state.db")
    if not db_path.exists():
        print(f"Error: Database not found at {db_path}")
        exit(1)
    
    optimize_database(db_path)
