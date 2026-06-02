#!/usr/bin/env python3
"""
Fetch and sync ICD-11 data from WHO API.

Usage:
    python scripts/fetch_icd11.py --data-dir data [--force]

Environment variables:
    ICD11_CLIENT_ID: OAuth2 client ID
    ICD11_CLIENT_SECRET: OAuth2 client secret
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
import yaml


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
) -> dict[str, Any]:
    """Make an authenticated request with rate limiting and retries."""
    # Check total timeout
    elapsed_hours = (time.time() - start_time) / 3600
    if elapsed_hours > TOTAL_TIMEOUT_HOURS:
        print(f"Total timeout exceeded ({TOTAL_TIMEOUT_HOURS}h)", file=sys.stderr)
        sys.exit(75)

    headers = {"Authorization": f"Bearer {token}", "Accept-Language": "en"}

    for attempt in range(MAX_RETRIES):
        try:
            time.sleep(RATE_LIMIT_DELAY)
            resp = session.get(url, headers=headers, timeout=30)

            if resp.status_code == 401:
                # Token expired, get new one (caller must handle this)
                raise requests.HTTPError("401 Unauthorized - token expired")

            if resp.status_code == 429 or resp.status_code >= 500:
                retry_after = int(resp.headers.get("Retry-After", 2**attempt))
                time.sleep(retry_after)
                continue

            resp.raise_for_status()
            return resp.json()  # type: ignore[no-any-return]

        except requests.RequestException:
            if attempt == MAX_RETRIES - 1:
                raise
            time.sleep(2**attempt)

    raise RuntimeError("Max retries exceeded")


def fetch_release_date(session: requests.Session, token: str, start_time: float) -> str:
    """Fetch the release date of ICD-11."""
    url = "https://id.who.int/icd/release/11"
    data = make_request(session, url, token, start_time)
    return data.get("releaseDate", "")  # type: ignore[no-any-return]


def should_sync(data_dir: Path, release_date: str, force: bool = False) -> bool:
    """Check if sync is needed based on release date."""
    metadata_file = data_dir / ".sync_metadata.json"

    if force:
        return True

    if not metadata_file.exists():
        return True

    try:
        with open(metadata_file, "r", encoding="utf-8") as f:
            metadata: dict[str, Any] = json.load(f)
        existing_date = metadata.get("release_date", "")
        return existing_date != release_date  # type: ignore[no-any-return]
    except Exception:
        return True


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
        "last_sync": datetime.now(timezone.utc).isoformat(),
    }
    with open(metadata_file, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)


def fetch_linearisation_tree(
    session: requests.Session,
    token: str,
    url: str,
    start_time: float,
    children_key: str = "child",
) -> list[dict[str, Any]]:
    """Recursively fetch the linearisation tree."""
    result: list[dict[str, Any]] = []

    data = make_request(session, url, token, start_time)

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
) -> dict[str, Any]:
    """Fetch a single foundation entity."""
    url = f"https://id.who.int/icd/entity/{entity_id}"
    return make_request(session, url, token, start_time)


def extract_code_from_title(title: str) -> str:
    """Extract ICD code from title string like '1B21.0 Plague'."""
    parts = title.split(" ", 1)
    return parts[0] if parts else title


def write_disease_yaml(category: dict[str, Any], output_path: Path) -> None:
    """Write disease YAML file."""
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
        "last_updated": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(
            yaml_data,
            f,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )


def write_foundation_yaml(entity: dict[str, Any], output_path: Path) -> None:
    """Write foundation entity YAML file."""
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

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(
            yaml_data,
            f,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )


def main(data_dir: Path, force: bool = False) -> int:
    """Main entry point."""
    # Check credentials
    client_id = os.environ.get("ICD11_CLIENT_ID", "")
    client_secret = os.environ.get("ICD11_CLIENT_SECRET", "")

    if not client_id or not client_secret:
        print(
            "Error: ICD11_CLIENT_ID and ICD11_CLIENT_SECRET environment variables required",
            file=sys.stderr,
        )
        return 1

    start_time = time.time()

    # Create session
    session = requests.Session()

    try:
        # Get token
        print("Obtaining OAuth2 token...")
        token = get_token(session, client_id, client_secret)
        print("Token obtained.")

        # Fetch release date
        print("Fetching release date...")
        release_date = fetch_release_date(session, token, start_time)
        print(f"Release date: {release_date}")

        # Check if sync needed
        if not should_sync(data_dir, release_date, force):
            print("Already up to date. Use --force to re-sync.")
            return 0

        print("Starting sync...")

        # Load state for resume
        state = load_state(data_dir)
        processed_ids: set[str] = set(state.get("processed", []))
        pending_ids: list[str] = state.get("pending", [])

        # Fetch linearisation tree
        if not pending_ids:
            print("Fetching MMS linearisation tree...")
            mms_url = "https://id.who.int/icd/release/11/mms"
            tree = fetch_linearisation_tree(session, token, mms_url, start_time)
            print(f"Fetched {len(tree)} entities from tree.")

            # Extract disease categories
            categories = extract_disease_categories(tree)
            print(f"Found {len(categories)} disease categories.")

            # Collect foundation references
            foundation_ids = collect_foundation_refs(tree)
            print(f"Found {len(foundation_ids)} foundation entity references.")

            # Build pending list: diseases first, then foundation
            pending_ids = [f"disease:{c.get('@id', '')}" for c in categories]
            pending_ids.extend([f"foundation:{fid}" for fid in foundation_ids])

            # Remove already processed
            pending_ids = [pid for pid in pending_ids if pid not in processed_ids]
            print(f"{len(pending_ids)} entities remaining to process.")

        # Process pending entities
        mms_dir = data_dir / "mms"
        foundation_dir = data_dir / "foundation"

        processed_count = 0
        for pending_id in pending_ids[:]:
            # Check timeout periodically
            if (time.time() - start_time) / 3600 > TOTAL_TIMEOUT_HOURS:
                print(f"Timeout reached. Saved state with {len(pending_ids)} remaining.")
                state["pending"] = pending_ids
                state["processed"] = list(processed_ids)
                save_state(data_dir, state)
                return 75

            if pending_id.startswith("disease:"):
                entity_uri = pending_id[8:]
                # Find category in tree (we need to refetch or store it)
                # For simplicity, we'll fetch each disease individually
                print(f"Processing disease: {entity_uri}")

                # Fetch the entity
                try:
                    entity_data = make_request(session, entity_uri, token, start_time)
                    output_path = mms_dir / f"{extract_code_from_title(entity_data.get('title', ''))}.yaml"
                    write_disease_yaml(entity_data, output_path)
                    print(f"  Written: {output_path.name}")
                except Exception as e:
                    print(f"  Error: {e}", file=sys.stderr)
                    continue

            elif pending_id.startswith("foundation:"):
                entity_id = pending_id[11:]
                print(f"Processing foundation: {entity_id}")

                try:
                    entity_data = fetch_foundation_entity(
                        session, token, entity_id, start_time
                    )
                    output_path = foundation_dir / f"{entity_id}.yaml"
                    write_foundation_yaml(entity_data, output_path)
                    print(f"  Written: {output_path.name}")
                except Exception as e:
                    print(f"  Error: {e}", file=sys.stderr)
                    continue

            processed_ids.add(pending_id)
            pending_ids.remove(pending_id)
            processed_count += 1

            # Save state periodically (every 10 entities)
            if processed_count % 10 == 0:
                state["pending"] = pending_ids
                state["processed"] = list(processed_ids)
                save_state(data_dir, state)

        # Clear state on success
        clear_state(data_dir)

        # Save metadata
        save_metadata(data_dir, release_date)

        print(f"Sync complete. Processed {processed_count} entities.")
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
