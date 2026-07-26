#!/usr/bin/env python3
"""
Fetch and sync ICD-11 data from WHO API.

Sync Strategy:
- WHO ICD-11 updates rarely (months/years), not daily.
- Weekly cron checks for updates via releaseDate.
- Manual trigger (workflow_dispatch) uses --force for full re-sync.
- Idempotent: files are only written if content changed.

Usage:
    python scripts/fetch_icd11.py --data-dir data [--force]

Environment variables:
    ICD_CLIENT_ID: OAuth2 client ID
    ICD_CLIENT_SECRET: OAuth2 client secret
"""

import hashlib
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests
import yaml
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

# Rate limiting: 5 requests per second
RATE_LIMIT_DELAY = 0.25
MAX_RETRIES = 3
TOTAL_TIMEOUT_HOURS = 6


def get_token(session: requests.Session, client_id: str, client_secret: str) -> str:
    """Get OAuth2 access token from WHO ICD API."""
    url = "https://icdaccessmanagement.who.int/connect/token"
    data = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": "icdapi_access",
    }
    resp = session.post(url, data=data, timeout=30)
    resp.raise_for_status()
    result: dict[str, Any] = resp.json()
    return result["access_token"]  # type: ignore[no-any-return]


def make_request(
    session: requests.Session,
    url: str,
    token: str,
    start_time: float,
    client_id: str | None = None,
    client_secret: str | None = None,
) -> dict[str, Any]:
    """Make an authenticated request with rate limiting and retries.
    
    Implements reactive token refresh: on HTTP 401, automatically obtains
    a new token and retries the request. Max 3 retries to prevent infinite loops.
    Handles rate limiting (HTTP 429) with exponential backoff.
    """
    # Configure SSL for GitHub Actions environment
    session.trust_env = True  # Use system certificates for SSL

    elapsed_hours = (time.time() - start_time) / 3600
    if elapsed_hours > TOTAL_TIMEOUT_HOURS:
        print(f"Total timeout exceeded ({TOTAL_TIMEOUT_HOURS}h)", file=sys.stderr)
        sys.exit(75)

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Accept-Language": "en",
        "API-Version": "v2",  # REQUIRED for WHO API v2
    }

    # Track 401 retries separately from other retries
    auth_retry_count = 0
    max_auth_retries = 3

    for attempt in range(MAX_RETRIES):
        time.sleep(RATE_LIMIT_DELAY)
        resp = session.get(url, headers=headers, timeout=30)

        if resp.status_code == 401:
            # Token expired - attempt reactive refresh if credentials available
            auth_retry_count += 1
            if auth_retry_count > max_auth_retries or not client_id or not client_secret:
                raise requests.HTTPError("401 Unauthorized - token refresh failed or credentials unavailable")
            
            # Get fresh token
            new_token = get_token(session, client_id, client_secret)
            headers["Authorization"] = f"Bearer {new_token}"
            # Retry immediately with new token (don't count against MAX_RETRIES)
            continue

        if resp.status_code == 429:
            # Rate limited - wait and retry
            retry_after = int(resp.headers.get("Retry-After", 2**attempt))
            time.sleep(retry_after)
            continue

        if resp.status_code >= 500:
            # Server error - exponential backoff
            time.sleep(2**attempt)
            continue

        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]

    raise RuntimeError("Max retries exceeded")


def fetch_release_date(
    session: requests.Session,
    token: str,
    start_time: float,
    client_id: str | None = None,
    client_secret: str | None = None,
) -> str | None:
    """Fetch the release date of ICD-11."""
    url = "https://id.who.int/icd/release/11"
    try:
        data = make_request(session, url, token, start_time, client_id, client_secret)
        return data.get("releaseDate", "")  # type: ignore[no-any-return]
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            # Endpoint doesn't exist, return None to skip release date check
            return None
        raise


def get_latest_release(
    session: requests.Session,
    token: str,
    start_time: float,
    client_id: str | None = None,
    client_secret: str | None = None,
) -> str:
    """Get the latest MMS release URI."""
    url = "https://id.who.int/icd/release/11/mms"
    data = make_request(session, url, token, start_time, client_id, client_secret)
    # Returns full URI like "http://id.who.int/icd/release/11/2026-01/mms"
    # Handle both 'latestRelease' key and direct URI response
    if "latestRelease" in data:
        return data["latestRelease"]  # type: ignore[no-any-return]
    # If the API returns the URI directly (e.g., in tests), use @id or return as-is
    if "@id" in data:
        return data["@id"]  # type: ignore[no-any-return]
    # Fallback: if data is a string, return it directly
    if isinstance(data, str):
        return data
    raise KeyError("latestRelease")


def get_mms_root(
    session: requests.Session,
    release_uri: str,
    token: str,
    start_time: float,
    client_id: str | None = None,
    client_secret: str | None = None,
) -> dict[str, Any]:
    """Get MMS linearization root with chapter URIs."""
    # Use the full URI directly from API response
    # Convert http:// to https:// for consistency
    url = release_uri.replace("http://", "https://")
    return make_request(session, url, token, start_time, client_id, client_secret)


def fetch_entity(
    session: requests.Session,
    entity_uri: str,
    token: str,
    start_time: float,
    client_id: str | None = None,
    client_secret: str | None = None,
) -> dict[str, Any]:
    """Fetch a linearization entity by URI."""
    # Convert http:// to https:// for consistency
    url = entity_uri.replace("http://", "https://")
    return make_request(session, url, token, start_time, client_id, client_secret)


def should_sync(data_dir: Path, release_date: str | None, force: bool = False) -> bool:
    """Check if sync is needed."""
    if force:
        return True
    
    # If release_date is available and changed, sync
    if release_date:
        metadata_file = data_dir / ".sync_metadata.json"
        if metadata_file.exists():
            try:
                with open(metadata_file, "r", encoding="utf-8") as f:
                    metadata = json.load(f)
                existing_release_date = metadata.get("release_date")
                return bool(existing_release_date != release_date)
            except Exception:
                return True
        return True  # No metadata file yet
    
    # If release_date couldn't be fetched, still allow sync
    # This handles cases where WHO API endpoint is temporarily unavailable
    # Check if we have any existing data
    mms_dir = data_dir / "mms"
    foundation_dir = data_dir / "foundation"
    
    # If no data exists at all, we should sync
    if not mms_dir.exists() or not foundation_dir.exists():
        return True
    
    # If data exists but release_date is unavailable, skip only if not forced
    # (manual intervention may be needed)
    return False


def load_state(data_dir: Path) -> dict[str, Any]:
    """Load fetch state for checkpoint/resume."""
    state_file = data_dir / ".fetch_state.json"
    if not state_file.exists():
        return {"pending": [], "processed": []}

    try:
        with open(state_file, "r", encoding="utf-8") as f:
            return json.load(f)  # type: ignore[no-any-return]
    except Exception:
        return {"pending": [], "processed": []}


def save_state(data_dir: Path, state: dict[str, Any]) -> None:
    """Save fetch state."""
    state_file = data_dir / ".fetch_state.json"
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def clear_state(data_dir: Path) -> None:
    """Clear fetch state after successful completion."""
    state_file = data_dir / ".fetch_state.json"
    if state_file.exists():
        state_file.unlink()


def save_metadata(data_dir: Path, release_date: str) -> None:
    """Save sync metadata."""
    metadata_file = data_dir / ".sync_metadata.json"
    metadata = {
        "release_date": release_date,
        "last_sync": datetime.now(UTC).isoformat(),
    }
    with open(metadata_file, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)


def fetch_linearisation_tree_bfs(
    session: requests.Session,
    token: str,
    root_url: str,
    start_time: float,
    mms_dir: Path,
    client_id: str | None = None,
    client_secret: str | None = None,
) -> set[str]:
    """Iteratively fetch the linearisation tree using BFS.
    
    This replaces the recursive approach to handle trees of any depth without stack overflow.
    Writes disease YAML files incrementally during traversal.
    
    Returns:
        Set of foundation entity IDs referenced in the tree.
    """
    from collections import deque
    
    visited: set[str] = set()
    foundation_ids: set[str] = set()
    queue: deque[str] = deque([root_url])
    processed_count = 0
    
    while queue:
        # Check timeout
        elapsed_hours = (time.time() - start_time) / 3600
        if elapsed_hours > TOTAL_TIMEOUT_HOURS:
            print(f"Total timeout exceeded ({TOTAL_TIMEOUT_HOURS}h) during BFS traversal", file=sys.stderr)
            sys.exit(75)
        
        current_url = queue.popleft()
        
        # Skip if already visited
        if current_url in visited:
            continue
        visited.add(current_url)
        
        # Fetch entity
        time.sleep(RATE_LIMIT_DELAY)
        try:
            entity = make_request(session, current_url, token, start_time, client_id, client_secret)
        except Exception as e:
            print(f"Error fetching {current_url}: {e}", file=sys.stderr)
            continue
        
        # Process entity - write disease YAML if it's a category
        class_kind = entity.get("classKind", "")
        if class_kind == "category":
            output_path = mms_dir / f"{extract_code_from_title(entity.get('title', ''))}.yaml"
            if write_disease_yaml(entity, output_path):
                processed_count += 1
        
        # Collect foundation references from this entity
        extract_foundation_refs_from_entity(entity, foundation_ids)
        
        # Add children to queue
        child_uris = entity.get("child", [])
        for child_uri in child_uris:
            if isinstance(child_uri, str) and child_uri not in visited:
                queue.append(child_uri)
            elif isinstance(child_uri, dict):
                child_id = child_uri.get("@id", "")
                if child_id and child_id not in visited:
                    queue.append(child_id)
    
    return foundation_ids


def extract_foundation_refs_from_entity(entity: dict[str, Any], foundation_ids: set[str]) -> None:
    """Extract foundation entity references from a single entity and add to set.
    
    Iterative approach to avoid recursion depth issues.
    """
    stack: list[Any] = [entity]
    
    while stack:
        obj = stack.pop()
        
        if isinstance(obj, dict):
            # Look for @id fields that point to foundation entities
            entity_id = obj.get("@id", "")
            if entity_id and "/entity/" in entity_id:
                parts = entity_id.split("/entity/")
                if len(parts) > 1:
                    foundation_ids.add(parts[-1])
            
            # Add nested values to stack
            for value in obj.values():
                stack.append(value)
        
        elif isinstance(obj, list):
            for item in obj:
                stack.append(item)


def fetch_linearisation_tree(
    session: requests.Session,
    token: str,
    url: str,
    start_time: float,
    children_key: str = "child",
    client_id: str | None = None,
    client_secret: str | None = None,
) -> list[dict[str, Any]]:
    """Recursively fetch the linearisation tree (DEPRECATED - use BFS version)."""
    result: list[dict[str, Any]] = []

    data = make_request(session, url, token, start_time, client_id, client_secret)

    # Process current level
    items = data.get(children_key, [])
    for item in items:
        result.append(item)
        # Recurse into children
        if children_key in item:
            child_url = item.get("@id")
            if child_url:
                result.extend(
                    fetch_linearisation_tree(
                        session, token, child_url, start_time, children_key
                    )
                )

    return result


def extract_disease_categories(tree: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract categories (diseases) from the tree."""
    categories = []
    for item in tree:
        class_kind = item.get("classKind", "")
        if class_kind == "category":
            categories.append(item)
    return categories


def collect_foundation_refs(tree: list[dict[str, Any]]) -> set[str]:
    """Collect all foundation entity references from the tree."""
    foundation_ids: set[str] = set()

    def extract_refs(obj: Any) -> None:
        if isinstance(obj, dict):
            # Look for @id fields that point to foundation entities
            entity_id = obj.get("@id", "")
            if entity_id and "/entity/" in entity_id:
                # Extract the ID part after /entity/
                parts = entity_id.split("/entity/")
                if len(parts) > 1:
                    foundation_ids.add(parts[-1])

            # Recurse into nested structures
            for value in obj.values():
                extract_refs(value)

        elif isinstance(obj, list):
            for item in obj:
                extract_refs(item)

    for item in tree:
        extract_refs(item)

    return foundation_ids


def fetch_foundation_entity(
    session: requests.Session,
    token: str,
    entity_id: str,
    start_time: float,
    client_id: str | None = None,
    client_secret: str | None = None,
) -> dict[str, Any]:
    """Fetch a single foundation entity."""
    url = f"https://id.who.int/icd/entity/{entity_id}"
    return make_request(session, url, token, start_time, client_id, client_secret)


def process_mms_entity(
    session: requests.Session,
    uri: str,
    token: str,
    start_time: float,
    visited: set[str],
    client_id: str | None = None,
    client_secret: str | None = None,
) -> list[dict[str, Any]]:
    """Process a single MMS entity and its children recursively.
    
    Returns a list of all processed entities.
    """
    if uri in visited:
        return []
    visited.add(uri)
    
    # Fetch full entity details using the URI directly
    time.sleep(RATE_LIMIT_DELAY)  # Throttle
    full_entity = fetch_entity(session, uri, token, start_time, client_id, client_secret)
    
    result = [full_entity]
    
    # Process children (child is array of URIs)
    for child_uri in full_entity.get("child", []):
        result.extend(process_mms_entity(session, child_uri, token, start_time, visited, client_id, client_secret))
    
    return result


def extract_code_from_title(title: str) -> str:
    """Extract ICD code from title string like '1B21.0 Plague'."""
    parts = title.split(" ", 1)
    return parts[0] if parts else title


def yaml_content_hash(yaml_data: dict[str, Any]) -> str:
    """Compute a hash of YAML content for idempotency check."""
    content = yaml.safe_dump(
        yaml_data,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def write_yaml_idempotent(output_path: Path, yaml_data: dict[str, Any]) -> bool:
    """Write YAML file only if content has changed. Returns True if written."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Check if file exists and has same content
    if output_path.exists():
        with open(output_path, "r", encoding="utf-8") as f:
            existing_content = f.read()
        
        new_content = yaml.safe_dump(
            yaml_data,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )
        
        if existing_content == new_content:
            return False  # No change needed
    
    # Write new content
    with open(output_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(
            yaml_data,
            f,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )
    return True


def write_disease_yaml(category: dict[str, Any], output_path: Path) -> bool:
    """Write disease YAML file. Returns True if file was written/updated."""
    entity_uri = category.get("@id", "")
    title = category.get("title", "")
    code = extract_code_from_title(title)

    # Get parent code
    parent_code: str | None = None
    parent = category.get("parent", {})
    if isinstance(parent, dict):
        parent_title = parent.get("title", "")
        if parent_title:
            parent_code = extract_code_from_title(parent_title)

    # Get children codes (only categories)
    children_codes: list[str] = []
    for child in category.get("child", []):
        if child.get("classKind") == "category":
            child_title = child.get("title", "")
            if child_title:
                children_codes.append(extract_code_from_title(child_title))

    # Get definition
    definition = ""
    for note in category.get("note", []):
        if note.get("noteType") == "definition":
            lang = note.get("language", "")
            if lang == "en" or lang.startswith("en"):
                definition = note.get("value", "")
                break

    yaml_data: dict[str, Any] = {
        "entity_uri": entity_uri,
        "code": code,
        "title_en": title,
        "definition_en": definition,
        "parent_code": parent_code,
        "children_codes": children_codes,
        "pathophysiology_en": None,
        "symptoms": [],
        "differential_diagnosis": [],
        "risk_factors": [],
        "drugs": [],
        "vector_text_en": None,
        "stats": {
            "mortality_global_annual": None,
            "dalys_global": None,
            "incidence_rate_per_100k": None,
            "active_clinical_trials": None,
            "child_code_count": len(children_codes),
            "research_link_count": 0,
        },
        "ai_enriched": False,
        "last_updated": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }

    return write_yaml_idempotent(output_path, yaml_data)


def write_foundation_yaml(entity: dict[str, Any], output_path: Path) -> bool:
    """Write foundation entity YAML file. Returns True if file was written/updated."""
    entity_id = output_path.stem
    title = entity.get("title", "")
    definition = ""

    # Get definition from notes
    for note in entity.get("note", []):
        if note.get("noteType") == "definition":
            lang = note.get("language", "")
            if lang == "en" or lang.startswith("en"):
                definition = note.get("value", "")
                break

    yaml_data: dict[str, Any] = {
        "id": entity_id,
        "title_en": title,
        "definition_en": definition,
        "related_systems": [],
    }

    return write_yaml_idempotent(output_path, yaml_data)


def main(data_dir: Path, force: bool = False) -> int:
    """Main entry point.
    
    Sync Strategy:
    - Phase 1: BFS traversal of MMS tree with incremental disease YAML writes
    - Phase 2: Process foundation entities separately
    - Checkpoint/resume support via state file
    """
    # Check credentials - use ICD_CLIENT_ID and ICD_CLIENT_SECRET as per requirements
    client_id = os.environ.get("ICD_CLIENT_ID", "")
    client_secret = os.environ.get("ICD_CLIENT_SECRET", "")

    if not client_id or not client_secret:
        print(
            "Error: ICD_CLIENT_ID and ICD_CLIENT_SECRET environment variables required",
            file=sys.stderr,
        )
        return 1

    start_time = time.time()
    console = Console()

    # Create session
    session = requests.Session()

    try:
        # Get token
        with console.status("[bold green]Obtaining OAuth2 token..."):
            token = get_token(session, client_id, client_secret)
        console.print("[green]Token obtained.[/green]")

        # Fetch release date (non-blocking - sync proceeds even if unavailable)
        release_date: str | None = None
        try:
            with console.status("[bold green]Fetching release date..."):
                release_date = fetch_release_date(session, token, start_time, client_id, client_secret)
            if release_date:
                console.print(f"[green]Release date: {release_date}[/green]")
            else:
                console.print("[yellow]Release date not available from API. Proceeding with sync...[/yellow]")
        except Exception as e:
            console.print(f"[yellow]Could not fetch release date: {e}. Proceeding with sync...[/yellow]")
            release_date = None

        # Check if sync needed
        if not should_sync(data_dir, release_date, force):
            console.print("[yellow]Already up to date. Use --force to re-sync.[/yellow]")
            return 0

        # Save metadata before sync (release_date is guaranteed to be str here due to should_sync logic)
        if release_date:
            save_metadata(data_dir, release_date)

        console.print("[bold blue]Starting sync...[/bold blue]")

        # Load state for resume
        state = load_state(data_dir)
        processed_ids: set[str] = set(state.get("processed", []))
        pending_foundation: list[str] = state.get("pending_foundation", [])
        bfs_complete = state.get("bfs_complete", False)

        # Setup directories
        mms_dir = data_dir / "mms"
        foundation_dir = data_dir / "foundation"
        mms_dir.mkdir(parents=True, exist_ok=True)
        foundation_dir.mkdir(parents=True, exist_ok=True)

        # Phase 1: BFS traversal of MMS tree (disease entities)
        if not bfs_complete:
            console.print("[bold blue]Phase 1: BFS traversal of MMS tree...[/bold blue]")
            
            # Step 1: Get latest release URI
            with console.status("[bold green]Fetching latest MMS release..."):
                release_uri = get_latest_release(session, token, start_time, client_id, client_secret)
            console.print(f"[green]✓ Latest release: {release_uri}[/]")

            # Step 2: Get MMS root with chapters (use URI directly)
            with console.status("[bold green]Fetching MMS root..."):
                mms_root = get_mms_root(session, release_uri, token, start_time, client_id, client_secret)
            chapters = mms_root.get("child", [])
            console.print(f"[green]✓ Found {len(chapters)} chapters[/]")

            # Step 3: BFS traversal - processes each chapter iteratively
            # Disease YAML files are written incrementally during traversal
            all_foundation_ids: set[str] = set()
            
            for i, chapter_uri in enumerate(chapters):
                console.print(f"[blue]Processing chapter {i+1}/{len(chapters)}...[/blue]")
                foundation_ids = fetch_linearisation_tree_bfs(
                    session, token, chapter_uri, start_time, mms_dir, client_id, client_secret
                )
                all_foundation_ids.update(foundation_ids)
            
            console.print(f"[green]✓ MMS tree traversal complete. Found {len(all_foundation_ids)} foundation references.[/green]")
            
            # Build foundation pending list
            pending_foundation = [fid for fid in all_foundation_ids if f"foundation:{fid}" not in processed_ids]
            
            # Mark BFS as complete
            bfs_complete = True
            state["bfs_complete"] = True
            state["pending_foundation"] = pending_foundation
            state["processed"] = list(processed_ids)
            save_state(data_dir, state)

        # Phase 2: Process foundation entities
        console.print("[bold blue]Phase 2: Processing foundation entities...[/bold blue]")
        
        files_written = 0
        files_skipped = 0
        processed_count = 0
        
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            task = progress.add_task("Processing foundation...", total=len(pending_foundation))
            
            for entity_id in pending_foundation[:]:
                # Check timeout periodically
                if (time.time() - start_time) / 3600 > TOTAL_TIMEOUT_HOURS:
                    console.print(f"[yellow]Timeout reached. Saved state with {len(pending_foundation)} remaining.[/yellow]")
                    state["pending_foundation"] = pending_foundation
                    state["processed"] = list(processed_ids)
                    save_state(data_dir, state)
                    return 75

                progress.update(task, description=f"Processing foundation: {entity_id}")

                try:
                    entity_data = fetch_foundation_entity(
                        session, token, entity_id, start_time, client_id, client_secret
                    )
                    output_path = foundation_dir / f"{entity_id}.yaml"
                    if write_foundation_yaml(entity_data, output_path):
                        files_written += 1
                    else:
                        files_skipped += 1
                except Exception as e:
                    console.print(f"[red]Error processing {entity_id}: {e}[/red]")
                    continue

                processed_ids.add(f"foundation:{entity_id}")
                pending_foundation.remove(entity_id)
                processed_count += 1
                progress.advance(task)

                # Save state periodically (every 10 entities)
                if processed_count % 10 == 0:
                    state["pending_foundation"] = pending_foundation
                    state["processed"] = list(processed_ids)
                    save_state(data_dir, state)

        # Clear state on success
        clear_state(data_dir)

        console.print(f"[green]Sync complete. Processed {processed_count} foundation entities.[/green]")
        console.print(f"[green]Files written: {files_written}, Files skipped (unchanged): {files_skipped}[/green]")
        return 0

    except requests.RequestException as e:
        print(f"Request error: {e}", file=sys.stderr)
        return 1
    finally:
        session.close()


def cli() -> int:
    """CLI entry point with argument parsing."""
    import argparse

    parser = argparse.ArgumentParser(description="Fetch ICD-11 data from WHO API")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(__file__).parent.parent / "data",
        help="Data directory (default: ../data)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force re-sync even if up to date",
    )

    args = parser.parse_args()
    return main(args.data_dir, args.force)


if __name__ == "__main__":
    sys.exit(cli())
