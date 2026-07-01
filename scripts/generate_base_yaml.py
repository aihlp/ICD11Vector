#!/usr/bin/env python3
"""
Generate base YAML files from SQLite database.

This script reads from sync_state.db and generates/updates base YAML files
in data/mms/{code}.yaml with the base schema fields.

Base YAML Schema includes:
- entity_uri, code, title_en, definition_en, parent_code, children_codes
- raw_paragraphs (extracted from API response)
- externally_enriched: false, ai_enriched: false

Usage:
    python scripts/generate_base_yaml.py --data-dir data
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml


def get_db_path(data_dir: Path) -> Path:
    """Get the path to the SQLite database file."""
    db_dir = data_dir / "db"
    db_dir.mkdir(parents=True, exist_ok=True)
    return db_dir / "sync_state.db"


def init_db(db_path: Path):
    """Initialize database connection."""
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def get_all_base_done_nodes(conn) -> list:
    """Get all nodes with BASE_DONE status."""
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM icd_nodes_state WHERE status = 'BASE_DONE'")
    return cursor.fetchall()


def extract_parent_code(raw_data: dict[str, Any]) -> str | None:
    """Extract parent code from raw API data."""
    # WHO API returns parent in various formats
    parent = raw_data.get("parent", {})
    
    if isinstance(parent, str):
        # Direct URI
        return extract_code_from_uri(parent)
    elif isinstance(parent, dict):
        # Could be {'@id': '...'} or language-keyed
        if "@id" in parent:
            return extract_code_from_uri(parent["@id"])
        # Try language keys
        for key in ["en", "en-US"]:
            if key in parent:
                val = parent[key]
                if isinstance(val, str):
                    return extract_code_from_uri(val)
                elif isinstance(val, dict) and "@id" in val:
                    return extract_code_from_uri(val["@id"])
    return None


def extract_code_from_uri(uri: str) -> str:
    """Extract ICD code from URI like 'https://id.who.int/icd/entity/1B21.0'."""
    if not uri:
        return ""
    # Split by '/' and take last part
    parts = uri.rstrip("/").split("/")
    return parts[-1] if parts else ""


def extract_children_codes(raw_data: dict[str, Any]) -> list[str]:
    """Extract children codes from raw API data."""
    children_codes = []
    children = raw_data.get("child", [])
    
    # Handle dict case (language-keyed or @list)
    if isinstance(children, dict):
        if "@list" in children:
            children = children["@list"]
        else:
            children = children.get("en", []) or children.get("en-US", []) or next(iter(children.values()), [])
    
    if isinstance(children, list):
        for child in children:
            if isinstance(child, str):
                code = extract_code_from_uri(child)
                if code:
                    children_codes.append(code)
            elif isinstance(child, dict):
                if "@id" in child:
                    code = extract_code_from_uri(child["@id"])
                    if code:
                        children_codes.append(code)
                elif "target" in child and isinstance(child["target"], dict) and "@id" in child["target"]:
                    code = extract_code_from_uri(child["target"]["@id"])
                    if code:
                        children_codes.append(code)
    
    return children_codes


def extract_raw_paragraphs(raw_data: dict[str, Any]) -> list[str]:
    """Extract text paragraphs from notes and other fields in raw API data."""
    paragraphs = []
    
    # Extract from notes
    for note in raw_data.get("note", []):
        value = note.get("value", "")
        if value and isinstance(value, str):
            paragraphs.append(value)
    
    return paragraphs


def generate_base_yaml(node: Any, mms_dir: Path) -> tuple[Path | None, bool]:
    """Generate/update base YAML file for a node.
    
    Returns tuple of (yaml_path, was_generated).
    was_generated is True if a new file was created, False if skipped/existed.
    """
    raw_data = json.loads(node["raw_data"])
    icd_code = node["icd_code"]
    
    if not icd_code:
        # Skip nodes without ICD code
        return (None, False)
    
    yaml_path = mms_dir / f"{icd_code}.yaml"
    
    # Check if file already exists
    if yaml_path.exists():
        with open(yaml_path, "r", encoding="utf-8") as f:
            existing_data = yaml.safe_load(f)
        
        # If file already has required fields, skip it (preserve existing data)
        if existing_data and existing_data.get("code"):
            return (yaml_path, False)
    
    # Build base YAML structure
    title_raw = raw_data.get("title", "")
    
    # Handle title as dict (language-keyed)
    if isinstance(title_raw, dict):
        title_en = title_raw.get("en", "") or title_raw.get("en-US", "")
        if not title_en and "@value" in title_raw:
            title_en = title_raw["@value"]
        if not title_en:
            for val in title_raw.values():
                if isinstance(val, str):
                    title_en = val
                    break
                elif isinstance(val, dict) and "@value" in val:
                    title_en = val["@value"]
                    break
            else:
                title_en = ""
    else:
        title_en = title_raw or ""
    
    # Extract definition from notes
    definition_en = ""
    for note in raw_data.get("note", []):
        if note.get("noteType") == "definition":
            lang = note.get("language", "")
            if lang == "en" or lang.startswith("en"):
                definition_en = note.get("value", "")
                break
    
    # Build entity URI
    entity_uri = node["uri"].replace("http://", "https://")
    
    children_codes = extract_children_codes(raw_data)
    
    yaml_data = {
        "entity_uri": entity_uri,
        "code": icd_code,
        "title_en": title_en,
        "definition_en": definition_en,
        "parent_code": extract_parent_code(raw_data),
        "children_codes": children_codes,
        "raw_paragraphs": extract_raw_paragraphs(raw_data),
        "pathophysiology_en": "",
        "symptoms": [],
        "differential_diagnosis": [],
        "risk_factors": [],
        "drugs": [],
        "vector_text_en": "",
        "stats": {
            "mortality_global_annual": None,
            "dalys_global": None,
            "incidence_rate_per_100k": None,
            "active_clinical_trials": None,
            "child_code_count": len(children_codes),
            "research_link_count": 0,
        },
        "externally_enriched": False,
        "ai_enriched": False,
        "last_updated": datetime.now().strftime("%Y-%m-%d"),
    }
    
    # Write YAML file
    mms_dir.mkdir(parents=True, exist_ok=True)
    with open(yaml_path, "w", encoding="utf-8") as f:
        yaml.dump(yaml_data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
    
    return (yaml_path, True)


def main(data_dir: Path) -> int:
    """Main entry point for generating base YAML files."""
    db_path = get_db_path(data_dir)
    mms_dir = data_dir / "mms"
    
    if not db_path.exists():
        print(f"Database not found at {db_path}. Run WHO client first.")
        return 1
    
    conn = init_db(db_path)
    
    try:
        nodes = get_all_base_done_nodes(conn)
        
        if not nodes:
            print("No BASE_DONE nodes found in database.")
            return 0
        
        generated_count = 0
        skipped_count = 0
        existed_count = 0
        
        for node in nodes:
            yaml_path, was_generated = generate_base_yaml(node, mms_dir)
            if yaml_path is None:
                skipped_count += 1
            elif was_generated:
                generated_count += 1
            else:
                existed_count += 1
        
        print(f"Generated/updated {generated_count + existed_count} base YAML files.")
        print(f"  Newly generated: {generated_count}")
        print(f"  Already existed: {existed_count}")
        print(f"Skipped {skipped_count} nodes (no ICD code).")
        
        return 0
        
    finally:
        conn.close()


def cli() -> int:
    """CLI entry point with argument parsing."""
    parser = argparse.ArgumentParser(description="Generate base YAML files from SQLite")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(__file__).parent.parent / "data",
        help="Data directory (default: ../data)",
    )
    
    args = parser.parse_args()
    return main(args.data_dir)


if __name__ == "__main__":
    sys.exit(cli())
