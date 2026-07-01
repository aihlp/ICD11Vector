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

# Fallback release versions in case latestRelease is unavailable
FALLBACK_RELEASE_VERSIONS = [
    "2026-01",
    "2025-01",
    "2024-01",
    "2023-01",
]

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


async def fetch_node_data_safe(
    session: aiohttp.ClientSession,
    uri: str,
    token: str,
    semaphore: asyncio.Semaphore | None = None,
    max_retries: int = MAX_RETRIES,
) -> tuple[str, dict[str, Any] | None]:
    """Fetch node data from WHO API with manual retry logic and proper error handling.
    
    Uses manual retry with exponential backoff instead of tenacity to avoid
    issues with aiohttp session state. Distinguishes between 4xx (no retry)
    and 5xx/timeout (retry) errors.
    
    Returns tuple of (uri, data) where data is None if failed.
    """
    # Use semaphore if provided to limit concurrency
    if semaphore:
        async with semaphore:
            return await _fetch_with_retry(session, uri, token, max_retries)
    else:
        return await _fetch_with_retry(session, uri, token, max_retries)


async def _fetch_with_retry(
    session: aiohttp.ClientSession,
    uri: str,
    token: str,
    max_retries: int,
) -> tuple[str, dict[str, Any] | None]:
    """Internal function to perform fetch with retry logic."""
    # Convert http:// to https:// for consistency
    url = uri.replace("http://", "https://")
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Accept-Language": "en",
        "API-Version": "v2",
    }
    
    last_error: Exception | None = None
    
    for attempt in range(max_retries):
        try:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)) as resp:
                status = resp.status
                
                if status == 200:
                    data = await resp.json()
                    console.print(f"[dim]✓ {url} ({status})[/dim]")
                    return (uri, data)
                
                elif 400 <= status < 500:
                    # Client error - don't retry
                    error_text = ""
                    try:
                        error_text = await resp.text()
                        error_text = error_text[:500] if error_text else ""
                    except Exception:
                        pass
                    
                    console.print(f"[red]Client error {status} for {url}: {error_text[:200]}[/red]")
                    return (uri, None)
                
                else:
                    # Server error (5xx) - retry
                    error_text = ""
                    try:
                        error_text = await resp.text()
                        error_text = error_text[:500] if error_text else ""
                    except Exception:
                        pass
                    
                    console.print(f"[yellow]Server error {status} for {url}, attempt {attempt+1}/{max_retries}[/yellow]")
                    if attempt < max_retries - 1:
                        wait_time = 2 ** attempt  # Exponential backoff
                        console.print(f"[dim]Waiting {wait_time}s before retry...[/dim]")
                        await asyncio.sleep(wait_time)
                    last_error = Exception(f"Server error {status}")
                    
        except asyncio.TimeoutError:
            console.print(f"[yellow]Timeout for {url}, attempt {attempt+1}/{max_retries}[/yellow]")
            if attempt < max_retries - 1:
                wait_time = 2 ** attempt
                console.print(f"[dim]Waiting {wait_time}s before retry...[/dim]")
                await asyncio.sleep(wait_time)
            last_error = asyncio.TimeoutError(f"Timeout after {attempt+1} attempts")
            
        except aiohttp.ClientError as e:
            # Network/connection error - may retry
            console.print(f"[yellow]Network error for {url}: {type(e).__name__}: {e}[/yellow]")
            if attempt < max_retries - 1:
                wait_time = 2 ** attempt
                console.print(f"[dim]Waiting {wait_time}s before retry...[/dim]")
                await asyncio.sleep(wait_time)
            last_error = e
            
        except Exception as e:
            # Unexpected error - log and return None (don't retry)
            console.print(f"[red]Unexpected error for {url}: {type(e).__name__}: {e}[/red]")
            return (uri, None)
    
    # All retries exhausted
    console.print(f"[red]All {max_retries} attempts failed for {url}[/red]")
    return (uri, None)


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
    
    # Track progress for checkpoint logging
    successful_fetches = 0
    failed_fetches = 0
    
    # Create aiohttp session with trust_env for proper SSL handling
    async with aiohttp.ClientSession(trust_env=True) as session:
        # Create tasks for all nodes in batch
        tasks = [
            fetch_node_data_safe(session, node["uri"], token, semaphore)
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
            failed_fetches += 1
            console.print(f"[yellow]Failed to process {uri}: {result}[/yellow]")
            continue
        
        # Success - result is (uri, node_data)
        _, node_data = result
        
        if node_data is None:
            failed_count += 1
            failed_fetches += 1
            # Leave node as PENDING for retry in next batch
            continue
        
        # Check if we got actual data (not empty response)
        if not node_data or (isinstance(node_data, dict) and len(node_data) == 0):
            console.print(f"[yellow]Empty response for {uri}, leaving as PENDING for retry[/yellow]")
            failed_count += 1
            # Leave node as PENDING for retry in next batch
            continue
        
        successful_fetches += 1
        
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
            
            # Tree Traversal: Collect child URIs for bulk insert BEFORE updating status
            child_uris = extract_child_uris(node_data)
            all_child_uris.extend(child_uris)
            
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
    console.print(f"[dim]API fetch stats: {successful_fetches} successful, {failed_fetches} failed[/dim]")
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
        
        # Create aiohttp session for token request with trust_env for SSL
        async with aiohttp.ClientSession(trust_env=True) as session:
            # Get OAuth token
            console.print("[bold green]Obtaining OAuth2 token...[/bold green]")
            token = await get_token(session, client_id, client_secret)
            console.print("[green]Token obtained.[/green]")
            
            # Check if DB is empty - if so, seed with root/chapter URIs
            if is_db_empty(conn):
                console.print("[yellow]Database empty - seeding initial queue...[/yellow]")
                
                try:
                    # Step 1: Fetch the MMS root to get chapters directly
                    semaphore = asyncio.Semaphore(1)
                    _, root_data = await fetch_node_data_safe(session, ICD11_ROOT_URI, token, semaphore)
                    
                    chapter_uris: list[str] = []
                    
                    if root_data:
                        # Step 2: Try to extract child URIs (chapters) from root response first
                        chapter_uris = extract_child_uris(root_data)
                        
                        if chapter_uris:
                            console.print(f"[green]Found {len(chapter_uris)} top-level chapters from root[/green]")
                        else:
                            # Step 3: No chapters in root, try latestRelease
                            latest_release_uri = root_data.get("latestRelease", "")
                            if latest_release_uri:
                                console.print(f"[yellow]No chapters in root, trying latestRelease: {latest_release_uri}[/yellow]")
                                latest_release_uri = latest_release_uri.replace("http://", "https://")
                                _, release_data = await fetch_node_data_safe(session, latest_release_uri, token, semaphore)
                                
                                if release_data:
                                    chapter_uris = extract_child_uris(release_data)
                                    if chapter_uris:
                                        console.print(f"[green]Found {len(chapter_uris)} chapters from latestRelease[/green]")
                    
                    # Step 4: Fallback - try known release versions if no chapters found
                    if not chapter_uris:
                        console.print("[yellow]Trying fallback release versions...[/yellow]")
                        for version in FALLBACK_RELEASE_VERSIONS:
                            version_url = f"https://id.who.int/icd/release/11/{version}/mms"
                            console.print(f"[dim]Trying: {version_url}[/dim]")
                            _, version_data = await fetch_node_data_safe(session, version_url, token, semaphore)
                            
                            if version_data:
                                chapter_uris = extract_child_uris(version_data)
                                if chapter_uris:
                                    console.print(f"[green]Found {len(chapter_uris)} chapters in {version}[/green]")
                                    break
                    
                    # Step 5: Insert discovered URIs or fallback to root
                    if chapter_uris:
                        insert_pending_nodes_bulk_ignore(conn, chapter_uris, "PENDING")
                        console.print(f"[green]Queue seeded with {len(chapter_uris)} pending nodes[/green]")
                    else:
                        # Last resort: insert root URI itself
                        console.print("[yellow]All methods failed, inserting root URI as fallback[/yellow]")
                        from core.db import insert_pending_node_ignore
                        insert_pending_node_ignore(conn, ICD11_ROOT_URI, "PENDING")
                        
                except Exception as e:
                    # Catch any exception during seed and use fallback
                    console.print(f"[red]Error during seed: {type(e).__name__}: {e}[/red]")
                    console.print("[yellow]Using fallback: inserting root URI[/yellow]")
                    from core.db import insert_pending_node_ignore
                    insert_pending_node_ignore(conn, ICD11_ROOT_URI, "PENDING")
        
        # Verify we have nodes to process after seed
        pending_count = count_nodes_by_status(conn, "PENDING")
        if pending_count == 0 and is_db_empty(conn):
            console.print("[red]Failed to seed initial queue - database still empty[/red]")
            return 2  # Exit code 2 for "no data"
        
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
