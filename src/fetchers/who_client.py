#!/usr/bin/env python3
"""
Iterative WHO ICD-11 sync using queue-based BFS traversal.

This script processes a small batch of ICD-11 nodes per run, discovers their children,
saves state to SQLite, and exits quickly. Designed for GitHub Actions reliability.

Sync Strategy:
- Processes exactly 500 PENDING nodes per run
- Discovers child URIs and adds them to the queue (INSERT OR IGNORE)
- Updates processed nodes to BASE_DONE status
- Exits gracefully after each batch

Usage:
    python scripts/who_client.py --data-dir data

Environment variables:
    ICD_CLIENT_ID: OAuth2 client ID
    ICD_CLIENT_SECRET: OAuth2 client secret
"""

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import requests
from rich.console import Console
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# Import from core.db module
from core.db import (
    init_db,
    get_db_path,
    is_db_empty,
    get_nodes_by_status,
    insert_pending_node_ignore,
    update_node_data,
    count_nodes_by_status,
)

# Configuration
BATCH_SIZE = 500
REQUEST_TIMEOUT = 60  # seconds
MAX_RETRIES = 3
RATE_LIMIT_DELAY = 0.2  # seconds between requests

# WHO API endpoints
WHO_TOKEN_URL = "https://icdaccessmanagement.who.int/connect/token"
WHO_MMS_RELEASE_URL = "https://id.who.int/icd/release/11/mms"

# Root URIs for ICD-11 MMS linearization chapters (28 top-level chapters)
# These are used to seed the queue if the database is empty
ICD11_ROOT_URI = "https://id.who.int/icd/release/11/mms"


console = Console()


def get_token(session: requests.Session, client_id: str, client_secret: str) -> str:
    """Get OAuth2 access token from WHO ICD API."""
    data = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": "icdapi_access",
    }
    resp = session.post(WHO_TOKEN_URL, data=data, timeout=30)
    resp.raise_for_status()
    result: dict[str, Any] = resp.json()
    return result["access_token"]  # type: ignore[no-any-return]


@retry(
    stop=stop_after_attempt(MAX_RETRIES),
    wait=wait_exponential(multiplier=1, min=4, max=10),
    retry=retry_if_exception_type((requests.RequestException, requests.Timeout)),
)
def fetch_node_data(
    session: requests.Session,
    uri: str,
    token: str,
) -> dict[str, Any]:
    """Fetch node data from WHO API with retries and timeout.
    
    Uses tenacity for automatic retries on transient failures.
    Raises exception after MAX_RETRIES attempts.
    """
    # Convert http:// to https:// for consistency
    url = uri.replace("http://", "https://")
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Accept-Language": "en",
        "API-Version": "v2",
    }
    
    resp = session.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()  # type: ignore[no-any-return]


def extract_icd_code(title: str) -> str:
    """Extract ICD code from title string like '1B21.0 Plague'."""
    parts = title.split(" ", 1)
    return parts[0] if parts else title


def extract_child_uris(node_data: dict[str, Any]) -> list[str]:
    """Extract child URIs from a node's API response.
    
    The WHO API returns children in the 'child' field as either:
    - A list of URIs (strings)
    - A list of objects with '@id' fields
    """
    children = node_data.get("child", [])
    child_uris: list[str] = []
    
    for child in children:
        if isinstance(child, str):
            # Direct URI string
            uri = child.replace("http://", "https://")
            child_uris.append(uri)
        elif isinstance(child, dict) and "@id" in child:
            # Object with @id field
            uri = child["@id"].replace("http://", "https://")
            child_uris.append(uri)
    
    return child_uris


def seed_initial_queue(conn: Any, session: requests.Session, token: str) -> None:
    """Seed the queue with root MMS URI or chapter URIs if DB is empty.
    
    Fetches the MMS root to get the 28 top-level chapter URIs and inserts
    them into the queue with PENDING status.
    """
    console.print("[bold blue]Seeding initial queue with ICD-11 MMS chapters...[/bold blue]")
    
    try:
        # Fetch the MMS root to get chapter URIs
        root_data = fetch_node_data(session, ICD11_ROOT_URI, token)
        
        # Get child URIs (the 28 chapters)
        chapter_uris = extract_child_uris(root_data)
        
        if not chapter_uris:
            # Fallback: insert the root URI itself
            console.print("[yellow]No chapters found, inserting root URI[/yellow]")
            insert_pending_node_ignore(conn, ICD11_ROOT_URI, "PENDING")
        else:
            console.print(f"[green]Found {len(chapter_uris)} top-level chapters[/green]")
            for uri in chapter_uris:
                insert_pending_node_ignore(conn, uri, "PENDING")
        
        console.print(f"[green]Queue seeded with {count_nodes_by_status(conn, 'PENDING')} pending nodes[/green]")
        
    except Exception as e:
        console.print(f"[red]Error seeding queue: {e}[/red]")
        # Fallback: insert root URI directly
        insert_pending_node_ignore(conn, ICD11_ROOT_URI, "PENDING")


def process_batch(conn: Any, session: requests.Session, token: str) -> int:
    """Process a batch of PENDING nodes.
    
    Returns the number of nodes successfully processed.
    """
    # Fetch batch of PENDING nodes
    pending_nodes = get_nodes_by_status(conn, "PENDING", BATCH_SIZE)
    
    if not pending_nodes:
        console.print("[green]Tree fully synced - no PENDING nodes remaining[/green]")
        return 0
    
    console.print(f"[bold blue]Processing batch of {len(pending_nodes)} nodes...[/bold blue]")
    
    processed_count = 0
    failed_count = 0
    
    for node in pending_nodes:
        uri = node["uri"]
        
        try:
            # Fetch node data from WHO API
            time.sleep(RATE_LIMIT_DELAY)  # Rate limiting
            node_data = fetch_node_data(session, uri, token)
            
            # Extract node details
            title = node_data.get("title", "")
            icd_code = extract_icd_code(title) if title else ""
            
            # Extract definition/description from notes
            description = ""
            for note in node_data.get("note", []):
                if note.get("noteType") == "definition":
                    lang = note.get("language", "")
                    if lang == "en" or lang.startswith("en"):
                        description = note.get("value", "")
                        break
            
            # Update current node with fetched data and mark as BASE_DONE
            update_node_data(
                conn,
                uri,
                icd_code=icd_code,
                title=title,
                description=description,
                raw_data=node_data,
                status="BASE_DONE",
            )
            
            # Crucial Step: Tree Traversal - Find child URIs and add to queue
            child_uris = extract_child_uris(node_data)
            for child_uri in child_uris:
                insert_pending_node_ignore(conn, child_uri, "PENDING")
            
            processed_count += 1
            
        except Exception as e:
            # Node failed after retries - leave as PENDING and continue
            failed_count += 1
            console.print(f"[yellow]Failed to process {uri}: {e}[/yellow]")
            continue
    
    # Commit transaction
    conn.commit()
    
    console.print(f"[green]Batch complete: {processed_count} processed, {failed_count} failed[/green]")
    console.print(f"[green]Remaining PENDING: {count_nodes_by_status(conn, 'PENDING')}[/green]")
    console.print(f"[green]Total BASE_DONE: {count_nodes_by_status(conn, 'BASE_DONE')}[/green]")
    
    return processed_count


def main(data_dir: Path) -> int:
    """Main entry point for iterative WHO sync."""
    # Check credentials
    client_id = os.environ.get("ICD_CLIENT_ID", "")
    client_secret = os.environ.get("ICD_CLIENT_SECRET", "")
    
    if not client_id or not client_secret:
        console.print("[red]Error: ICD_CLIENT_ID and ICD_CLIENT_SECRET environment variables required[/red]")
        return 1
    
    db_path = get_db_path(data_dir)
    console.print(f"[dim]Using database: {db_path}[/dim]")
    
    # Initialize database
    conn = init_db(db_path)
    
    # Create session
    session = requests.Session()
    
    start_time = time.time()
    
    try:
        # Get OAuth token
        console.print("[bold green]Obtaining OAuth2 token...[/bold green]")
        token = get_token(session, client_id, client_secret)
        console.print("[green]Token obtained.[/green]")
        
        # Check if DB is empty - if so, seed with root/chapter URIs
        if is_db_empty(conn):
            console.print("[yellow]Database empty - seeding initial queue...[/yellow]")
            seed_initial_queue(conn, session, token)
        
        # Process one batch
        processed = process_batch(conn, session, token)
        
        if processed == 0:
            # No pending nodes - sync complete
            return 0
        
        elapsed = time.time() - start_time
        console.print(f"[dim]Batch completed in {elapsed:.1f} seconds[/dim]")
        
        return 0
        
    except requests.RequestException as e:
        console.print(f"[red]Request error: {e}[/red]")
        return 1
    except Exception as e:
        console.print(f"[red]Unexpected error: {e}[/red]")
        return 1
    finally:
        conn.close()
        session.close()


def cli() -> int:
    """CLI entry point with argument parsing."""
    import argparse
    
    parser = argparse.ArgumentParser(description="Iterative WHO ICD-11 sync (queue-based)")
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
