#!/usr/bin/env python3
"""
Async iterative WHO ICD-11 sync using queue-based BFS traversal.

This script processes batches of ICD-11 nodes per run using asyncio and aiohttp,
discovers their children, saves state to SQLite, and exits quickly.
Designed for GitHub Actions reliability with maximum throughput.

Sync Strategy:
- Processes exactly 2500 PENDING nodes per run (batch size)
- Uses asyncio.gather() with semaphore-limited concurrency (50 parallel requests)
- Discovers child URIs and bulk-adds them to the queue (executemany INSERT OR IGNORE)
- Updates processed nodes to BASE_DONE status
- Exits gracefully after each batch

Usage:
    python scripts/who_client.py --data-dir data

Environment variables:
    ICD_CLIENT_ID: OAuth2 client ID
    ICD_CLIENT_SECRET: OAuth2 client secret
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import aiohttp
from rich.console import Console
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from tenacity.asyncio import AsyncRetrying

# Import from core.db module
from core.db import (
    init_db,
    get_db_path,
    is_db_empty,
    get_nodes_by_status,
    insert_pending_nodes_bulk_ignore,
    update_node_data,
    count_nodes_by_status,
    detect_stuck_state,
    recover_from_stuck_state,
)

# Configuration
BATCH_SIZE = 2500  # Increased batch size for better throughput
MAX_CONCURRENT_REQUESTS = 50  # Semaphore limit to avoid API rate limiting
REQUEST_TIMEOUT = 60  # seconds
MAX_RETRIES = 3
RATE_LIMIT_DELAY = 0.1  # seconds between requests (reduced due to parallelism)

# WHO API endpoints
WHO_TOKEN_URL = "https://icdaccessmanagement.who.int/connect/token"
WHO_MMS_RELEASE_URL = "https://id.who.int/icd/release/11/mms"

# Root URIs for ICD-11 MMS linearization chapters (28 top-level chapters)
# These are used to seed the queue if the database is empty
ICD11_ROOT_URI = "https://id.who.int/icd/release/11/mms"


console = Console()


async def get_token(session: aiohttp.ClientSession, client_id: str, client_secret: str) -> str:
    """Get OAuth2 access token from WHO ICD API."""
    data = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": "icdapi_access",
    }
    async with session.post(WHO_TOKEN_URL, data=data, timeout=aiohttp.ClientTimeout(total=30)) as resp:
        resp.raise_for_status()
        result: dict[str, Any] = await resp.json()
        return result["access_token"]  # type: ignore[no-any-return]


@retry(
    stop=stop_after_attempt(MAX_RETRIES),
    wait=wait_exponential(multiplier=1, min=4, max=10),
    retry=retry_if_exception_type(Exception),
)
async def fetch_node_data_async(
    session: aiohttp.ClientSession,
    uri: str,
    token: str,
    semaphore: asyncio.Semaphore,
) -> tuple[str, dict[str, Any] | None]:
    """Fetch node data from WHO API asynchronously with retries and timeout.
    
    Uses tenacity for automatic retries on transient failures.
    Returns tuple of (uri, data) where data is None if failed.
    """
    async with semaphore:
        # Convert http:// to https:// for consistency
        url = uri.replace("http://", "https://")
        
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Accept-Language": "en",
            "API-Version": "v2",
        }
        
        try:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)) as resp:
                resp.raise_for_status()
                data = await resp.json()
                return (uri, data)  # type: ignore[no-any-return]
        except Exception as e:
            # Re-raise to trigger tenacity retry
            raise e


def extract_icd_code(title: str | dict[str, Any]) -> str:
    """Extract ICD code from title string like '1B21.0 Plague'.
    
    The WHO API may return title as either:
    - A simple string: '1B21.0 Plague'
    - A dict with language codes: {'en': '1B21.0 Plague', 'fr': '...'}
    - A dict with @value structure: {'@language': 'en', '@value': '1B21.0 Plague'}
    """
    if isinstance(title, dict):
        # Try common patterns for WHO API title dicts
        # Pattern 1: Direct language keys {'en': '...', 'fr': '...'}
        title_str = title.get('en', '') or title.get('en-US', '')
        
        # Pattern 2: JSON-LD @value structure {'@language': 'en', '@value': '...'}
        if not title_str and '@value' in title:
            title_str = title['@value']
        
        # Pattern 3: Nested structure - take first non-dict value
        if not title_str or not isinstance(title_str, str):
            for val in title.values():
                if isinstance(val, str):
                    title_str = val
                    break
                elif isinstance(val, dict) and '@value' in val:
                    title_str = val['@value']
                    break
            else:
                title_str = ''
    else:
        title_str = title
    
    if not title_str or not isinstance(title_str, str):
        return ''
    
    parts = title_str.split(" ", 1)
    return parts[0] if parts else title_str


def extract_child_uris(node_data: dict[str, Any]) -> list[str]:
    """Extract child URIs from a node's API response.
    
    The WHO API returns children in multiple fields:
    - 'child' field: as either:
        - A list of URIs (strings)
        - A list of objects with '@id' fields
        - A dict with language-keyed values containing objects with '@id'
        - A dict with @list structure containing items with '@id'
    - 'release' field: array of release version URIs (for root nodes)
    - 'latestRelease': single URI to latest release version
    
    Returns all discovered URIs for queue-based traversal.
    """
    child_uris: list[str] = []
    
    # Process 'child' field
    children = node_data.get("child", [])
    
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
                child_uris.append(uri)
            elif isinstance(child, dict):
                # Object with @id field (JSON-LD reference)
                if "@id" in child:
                    uri = child["@id"].replace("http://", "https://")
                    child_uris.append(uri)
                # Check for nested structures where @id might be in a sub-object
                elif "target" in child and isinstance(child["target"], dict):
                    target = child["target"]
                    if "@id" in target:
                        uri = target["@id"].replace("http://", "https://")
                        child_uris.append(uri)
                # Check for embedded entity with @id at top level of nested dict
                elif "@graph" in child:
                    graph = child["@graph"]
                    if isinstance(graph, list) and len(graph) > 0:
                        for item in graph:
                            if isinstance(item, dict) and "@id" in item:
                                uri = item["@id"].replace("http://", "https://")
                                child_uris.append(uri)
    
    # Process 'release' field (array of release version URIs)
    releases = node_data.get("release", [])
    if isinstance(releases, list):
        for release in releases:
            if isinstance(release, str):
                uri = release.replace("http://", "https://")
                if uri not in child_uris:
                    child_uris.append(uri)
            elif isinstance(release, dict) and "@id" in release:
                uri = release["@id"].replace("http://", "https://")
                if uri not in child_uris:
                    child_uris.append(uri)
    
    # Process 'latestRelease' field (single URI)
    latest_release = node_data.get("latestRelease", "")
    if isinstance(latest_release, str) and latest_release:
        uri = latest_release.replace("http://", "https://")
        if uri not in child_uris:
            child_uris.append(uri)
    elif isinstance(latest_release, dict) and "@id" in latest_release:
        uri = latest_release["@id"].replace("http://", "https://")
        if uri not in child_uris:
            child_uris.append(uri)
    
    return child_uris


def seed_initial_queue(conn: Any, token: str) -> None:
    """Seed the queue with root MMS URI or chapter URIs if DB is empty.
    
    Note: This is now synchronous - the caller handles the HTTP request.
    """
    console.print("[bold blue]Seeding initial queue with ICD-11 MMS chapters...[/bold blue]")


async def process_batch_async(
    conn: Any,
    pending_nodes: list,
    token: str,
) -> tuple[int, int]:
    """Process a batch of PENDING nodes asynchronously.
    
    Returns tuple of (processed_count, failed_count).
    """
    console.print(f"[bold blue]Processing batch of {len(pending_nodes)} nodes...[/bold blue]")
    
    # Create semaphore to limit concurrent requests
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
    
    # Create aiohttp session
    async with aiohttp.ClientSession() as session:
        # Create tasks for all nodes in batch
        tasks = [
            fetch_node_data_async(session, node["uri"], token, semaphore)
            for node in pending_nodes
        ]
        
        # Execute all tasks concurrently with error handling
        results = await asyncio.gather(*tasks, return_exceptions=True)
    
    # Process results
    processed_count = 0
    failed_count = 0
    all_child_uris: list[str] = []  # Collect all child URIs for bulk insert
    
    for i, result in enumerate(results):
        node = pending_nodes[i]
        uri = node["uri"]
        
        if isinstance(result, Exception):
            # Task failed
            failed_count += 1
            console.print(f"[yellow]Failed to process {uri}: {result}[/yellow]")
            continue
        
        # Success - result is (uri, node_data)
        _, node_data = result
        
        if node_data is None:
            failed_count += 1
            continue
        
        try:
            # Extract node details
            title_raw = node_data.get("title", "")
            
            # Title for storage - convert dict to string if needed (same logic as extract_icd_code)
            if isinstance(title_raw, dict):
                # Try common patterns for WHO API title dicts
                title = title_raw.get('en', '') or title_raw.get('en-US', '')
                if not title and '@value' in title_raw:
                    title = title_raw['@value']
                if not title or not isinstance(title, str):
                    for val in title_raw.values():
                        if isinstance(val, str):
                            title = val
                            break
                        elif isinstance(val, dict) and '@value' in val:
                            title = val['@value']
                            break
                    else:
                        title = ''
            else:
                title = title_raw
            
            icd_code = extract_icd_code(title_raw) if title_raw else ""
            
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
            
            # Tree Traversal: Collect child URIs for bulk insert
            child_uris = extract_child_uris(node_data)
            all_child_uris.extend(child_uris)
            
            processed_count += 1
            
        except Exception as e:
            # Node failed during processing - leave as PENDING and continue
            failed_count += 1
            console.print(f"[yellow]Error processing {uri}: {e}[/yellow]")
            continue
    
    # Bulk insert all discovered child URIs at once (reduces disk I/O)
    if all_child_uris:
        inserted = insert_pending_nodes_bulk_ignore(conn, all_child_uris, "PENDING")
        console.print(f"[green]Added {inserted} new child nodes to queue[/green]")
    
    # Commit transaction
    conn.commit()
    
    console.print(f"[green]Batch complete: {processed_count} processed, {failed_count} failed[/green]")
    console.print(f"[green]Remaining PENDING: {count_nodes_by_status(conn, 'PENDING')}[/green]")
    console.print(f"[green]Total BASE_DONE: {count_nodes_by_status(conn, 'BASE_DONE')}[/green]")
    
    return (processed_count, failed_count)


async def main_async(data_dir: Path) -> int:
    """Main entry point for async iterative WHO sync."""
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
    
    start_time = time.time()
    
    try:
        # Check for stuck/dead database state BEFORE processing
        if detect_stuck_state(conn):
            console.print("[yellow]Detected stuck database state (no PENDING nodes but DB not empty)[/yellow]")
            console.print("[blue]Attempting recovery...[/blue]")
            recovered = recover_from_stuck_state(conn)
            console.print(f"[green]Recovery complete: added {recovered} nodes to queue[/green]")
        
        # Create aiohttp session for token request
        async with aiohttp.ClientSession() as session:
            # Get OAuth token
            console.print("[bold green]Obtaining OAuth2 token...[/bold green]")
            token = await get_token(session, client_id, client_secret)
            console.print("[green]Token obtained.[/green]")
            
            # Check if DB is empty - if so, seed with root/chapter URIs
            if is_db_empty(conn):
                console.print("[yellow]Database empty - seeding initial queue...[/yellow]")
                
                # Fetch the MMS root to get the latest release URI
                semaphore = asyncio.Semaphore(1)
                async with aiohttp.ClientSession() as seed_session:
                    _, root_data = await fetch_node_data_async(seed_session, ICD11_ROOT_URI, token, semaphore)
                
                if root_data:
                    # Extract latestRelease URI and fetch that version to get chapters
                    latest_release_uri = root_data.get("latestRelease", "")
                    
                    if latest_release_uri:
                        console.print(f"[blue]Fetching latest release: {latest_release_uri}[/blue]")
                        # Convert to https
                        latest_release_uri = latest_release_uri.replace("http://", "https://")
                        
                        # Fetch the specific release to get chapter URIs
                        _, release_data = await fetch_node_data_async(seed_session, latest_release_uri, token, semaphore)
                        
                        if release_data:
                            chapter_uris = extract_child_uris(release_data)
                            
                            if not chapter_uris:
                                # Fallback: insert the release URI itself
                                console.print("[yellow]No chapters found, inserting release URI[/yellow]")
                                from core.db import insert_pending_node_ignore
                                insert_pending_node_ignore(conn, latest_release_uri, "PENDING")
                            else:
                                console.print(f"[green]Found {len(chapter_uris)} top-level chapters[/green]")
                                insert_pending_nodes_bulk_ignore(conn, chapter_uris, "PENDING")
                            
                            console.print(f"[green]Queue seeded with {count_nodes_by_status(conn, 'PENDING')} pending nodes[/green]")
                        else:
                            # Fallback: insert release URI directly
                            from core.db import insert_pending_node_ignore
                            insert_pending_node_ignore(conn, latest_release_uri, "PENDING")
                    else:
                        # No latestRelease found, fallback to root URI
                        console.print("[yellow]No latestRelease found, using root URI[/yellow]")
                        from core.db import insert_pending_node_ignore
                        insert_pending_node_ignore(conn, ICD11_ROOT_URI, "PENDING")
                else:
                    # Fallback: insert root URI directly
                    from core.db import insert_pending_node_ignore
                    insert_pending_node_ignore(conn, ICD11_ROOT_URI, "PENDING")
        
        # Fetch batch of PENDING nodes
        pending_nodes = get_nodes_by_status(conn, "PENDING", BATCH_SIZE)
        
        if not pending_nodes:
            # After recovery check, still no pending nodes - either complete or truly stuck
            base_done_count = count_nodes_by_status(conn, "BASE_DONE")
            if base_done_count > 0:
                console.print(f"[green]Sync complete: {base_done_count} nodes processed[/green]")
            else:
                console.print("[yellow]No nodes to process and no completed nodes - database may need manual reset[/yellow]")
            return 0
        
        # Process one batch asynchronously
        processed, failed = await process_batch_async(conn, pending_nodes, token)
        
        # Log progress checkpoint
        elapsed = time.time() - start_time
        remaining = count_nodes_by_status(conn, "PENDING")
        total_done = count_nodes_by_status(conn, "BASE_DONE")
        console.print(f"[dim]Checkpoint: {elapsed:.1f}s elapsed, {remaining} pending, {total_done} completed[/dim]")
        console.print(f"[dim]Batch completed in {elapsed:.1f} seconds[/dim]")
        
        return 0
        
    except aiohttp.ClientError as e:
        console.print(f"[red]Request error: {e}[/red]")
        return 1
    except Exception as e:
        console.print(f"[red]Unexpected error: {e}[/red]")
        return 1
    finally:
        conn.close()


def main(data_dir: Path) -> int:
    """Synchronous wrapper for async main."""
    return asyncio.run(main_async(data_dir))


def cli() -> int:
    """CLI entry point with argument parsing."""
    import argparse
    
    parser = argparse.ArgumentParser(description="Async iterative WHO ICD-11 sync (queue-based)")
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
