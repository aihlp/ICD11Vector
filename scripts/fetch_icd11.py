#!/usr/bin/env python3
"""
Fetch ICD-11 MMS entities from WHO API and convert to repository YAML format.

Usage:
    python scripts/fetch_icd11.py [--limit N] [--resume] [--checkpoint PATH] [--root PATH]

Environment variables required:
    ICD11_CLIENT_ID: WHO API client ID
    ICD11_CLIENT_SECRET: WHO API client secret
"""

import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import httpx
import yaml  # type: ignore[import-untyped]


class RateLimiter:
    """Rate limiter for API requests."""

    def __init__(
        self,
        requests_per_second: float,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.min_interval = 1.0 / requests_per_second
        self.last_request: float | None = None
        self._sleep = sleep

    def wait(self) -> None:
        """Wait if necessary to maintain rate limit."""
        if self.last_request is not None:
            elapsed = time.time() - self.last_request
            if elapsed < self.min_interval:
                self._sleep(self.min_interval - elapsed)
        self.last_request = time.time()


class ICD11Client:
    """Client for WHO ICD-11 API."""

    TOKEN_URL = "https://icdaccessmanagement.who.int/api/token"
    API_BASE = "https://id.who.int/icd/release/11/mms"

    def __init__(self, client_id: str, client_secret: str, rate_limiter: RateLimiter | None = None) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self._token: str | None = None
        self._token_expires_at: datetime | None = None
        self.rate_limiter = rate_limiter or RateLimiter(5.0)
        self._client = httpx.Client(timeout=30.0)

    def get_token(self) -> str:
        """Get OAuth2 access token using client credentials flow."""
        if self._token and self._token_expires_at and datetime.now(timezone.utc) < self._token_expires_at:
            return self._token

        self.rate_limiter.wait()
        response = self._client.post(
            self.TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
        )
        response.raise_for_status()
        data = response.json()
        self._token = data["access_token"]
        # Token expires in 3600 seconds per WHO docs; use small buffer
        expires_in = data.get("expires_in", 3600)
        self._token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in - 60)
        return self._token

    def _get_headers(self) -> dict[str, str]:
        """Get headers with authorization token."""
        return {
            "Authorization": f"Bearer {self.get_token()}",
            "Accept-Language": "en",
        }

    def get_entity(self, entity_uri: str) -> dict[str, Any]:
        """Fetch a single entity by URI."""
        self.rate_limiter.wait()
        response = self._client.get(entity_uri, headers=self._get_headers())
        response.raise_for_status()
        return response.json()  # type: ignore[no-any-return]

    def get_mms_root(self) -> dict[str, Any]:
        """Get the MMS root entity to discover child codes."""
        self.rate_limiter.wait()
        response = self._client.get(self.API_BASE, headers=self._get_headers())
        response.raise_for_status()
        return response.json()  # type: ignore[no-any-return]

    def get_children(self, parent_uri: str) -> list[dict[str, Any]]:
        """Get child entities of a parent."""
        self.rate_limiter.wait()
        url = f"{parent_uri}/children"
        response = self._client.get(url, headers=self._get_headers())
        response.raise_for_status()
        return response.json().get("childEntities", [])  # type: ignore[no-any-return]


def load_yaml(file_path: Path) -> dict[str, Any] | None:
    """Load a YAML file, returning None if it doesn't exist."""
    if not file_path.exists():
        return None
    with open(file_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)  # type: ignore[no-any-return]


def save_yaml_atomic(file_path: Path, data: dict[str, Any]) -> None:
    """Save data to YAML file atomically."""
    file_path.parent.mkdir(parents=True, exist_ok=True)

    # Write to temporary file in same directory
    fd, temp_path = tempfile.mkstemp(suffix=".yaml", dir=file_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.dump(
                data,
                f,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=True,
                width=100,
            )
        # Atomic rename
        os.replace(temp_path, file_path)
    except Exception:
        # Clean up temp file on error
        if os.path.exists(temp_path):
            os.unlink(temp_path)
        raise


def create_empty_disease_yaml() -> dict[str, Any]:
    """Create empty disease YAML with all required fields."""
    return {
        "ai_enriched": False,
        "children_codes": [],
        "code": "",
        "definition_en": "",
        "differential_diagnosis": [],
        "drugs": [],
        "entity_uri": "",
        "last_updated": "",
        "parent_code": None,
        "pathophysiology_en": "",
        "risk_factors": [],
        "stats": {
            "active_clinical_trials": None,
            "child_code_count": 0,
            "dalys_global": None,
            "incidence_rate_per_100k": None,
            "mortality_global_annual": None,
            "research_link_count": 0,
        },
        "symptoms": [],
        "title_en": "",
        "vector_text_en": "",
    }


def normalize_who_entity_to_disease(entity: dict[str, Any]) -> dict[str, Any]:
    """Convert WHO API entity to repository disease YAML format."""
    disease = create_empty_disease_yaml()

    # Extract basic fields
    disease["entity_uri"] = entity.get("@id", "")
    disease["code"] = entity.get("code", "")
    
    # Title - may be in different formats
    title_info = entity.get("title", {})
    if isinstance(title_info, str):
        disease["title_en"] = title_info
    elif isinstance(title_info, dict):
        disease["title_en"] = title_info.get("@value", "")

    # Definition
    definition_info = entity.get("definition", {})
    if isinstance(definition_info, str):
        disease["definition_en"] = definition_info
    elif isinstance(definition_info, dict):
        disease["definition_en"] = definition_info.get("@value", "")

    # Parent code
    parent = entity.get("parent", {})
    if isinstance(parent, dict):
        parent_code = parent.get("code", "")
        disease["parent_code"] = parent_code if parent_code else None
    elif isinstance(parent, str):
        disease["parent_code"] = parent if parent else None

    # Children codes - extract from childEntities if present
    children = entity.get("childEntities", [])
    disease["children_codes"] = [child.get("code", "") for child in children if child.get("code")]

    # Set last_updated to current UTC time
    disease["last_updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    return disease


def merge_existing_fields(new_data: dict[str, Any], existing: dict[str, Any]) -> dict[str, Any]:
    """Merge existing enriched fields into new data without overwriting them."""
    # Fields to preserve (not overwritten by WHO data)
    preserve_fields = [
        "pathophysiology_en",
        "symptoms",
        "differential_diagnosis",
        "risk_factors",
        "drugs",
        "vector_text_en",
        "stats",
        "ai_enriched",
    ]

    for field in preserve_fields:
        if field in existing:
            new_data[field] = existing[field]

    return new_data


def create_empty_symptom_yaml(symptom_id: str) -> dict[str, Any]:
    """Create empty symptom YAML with all required fields."""
    return {
        "definition_en": "",
        "id": symptom_id,
        "related_systems": [],
        "title_en": "",
    }


def normalize_who_foundation_entity(entity: dict[str, Any], symptom_id: str) -> dict[str, Any]:
    """Convert WHO foundation entity to repository symptom YAML format."""
    symptom = create_empty_symptom_yaml(symptom_id)

    # Title
    title_info = entity.get("title", {})
    if isinstance(title_info, str):
        symptom["title_en"] = title_info
    elif isinstance(title_info, dict):
        symptom["title_en"] = title_info.get("@value", "")

    # Definition
    definition_info = entity.get("definition", {})
    if isinstance(definition_info, str):
        symptom["definition_en"] = definition_info
    elif isinstance(definition_info, dict):
        symptom["definition_en"] = definition_info.get("@value", "")

    # Related systems - extract from classType or similar
    # This is heuristic; WHO API structure varies
    class_type = entity.get("classType", {})
    if isinstance(class_type, dict):
        label = class_type.get("label", {})
        if isinstance(label, str):
            symptom["related_systems"].append(label)
        elif isinstance(label, dict):
            val = label.get("@value", "")
            if val:
                symptom["related_systems"].append(val)

    return symptom


def load_checkpoint(checkpoint_path: Path) -> dict[str, Any]:
    """Load checkpoint file."""
    if not checkpoint_path.exists():
        return {"processed_entity_uris": [], "last_entity_uri": None, "updated_at": ""}
    with open(checkpoint_path, "r", encoding="utf-8") as f:
        return json.load(f)  # type: ignore[no-any-return]


def save_checkpoint(checkpoint_path: Path, checkpoint: dict[str, Any]) -> None:
    """Save checkpoint file atomically."""
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    fd, temp_path = tempfile.mkstemp(suffix=".json", dir=checkpoint_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(checkpoint, f, indent=2, sort_keys=True)
        os.replace(temp_path, checkpoint_path)
    except Exception:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
        raise


def validate_repository(base_dir: Path) -> list[str]:
    """Import and call validate_repository from validate.py."""
    # Import dynamically to avoid circular imports
    validate_module_path = base_dir / "scripts" / "validate.py"
    if not validate_module_path.exists():
        return []

    import importlib.util
    spec = importlib.util.spec_from_file_location("validate_module", validate_module_path)
    if not spec or not spec.loader:
        return []

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    if hasattr(module, "validate_repository"):
        return module.validate_repository(base_dir)  # type: ignore[no-any-return]
    return []


def fetch_and_save_entities(
    base_dir: Path,
    limit: int | None = None,
    resume: bool = False,
    checkpoint_path: Path | None = None,
    timeout_hours: float = 6.0,
) -> tuple[int, int]:
    """
    Fetch ICD-11 entities and save as YAML files.

    Returns:
        Tuple of (entities_written, entities_skipped)
    """
    # Get credentials from environment
    client_id = os.environ.get("ICD11_CLIENT_ID")
    client_secret = os.environ.get("ICD11_CLIENT_SECRET")

    if not client_id:
        print("Error: ICD11_CLIENT_ID environment variable is required.", file=sys.stderr)
        sys.exit(1)
    if not client_secret:
        print("Error: ICD11_CLIENT_SECRET environment variable is required.", file=sys.stderr)
        sys.exit(1)

    # Setup paths
    mms_dir = base_dir / "data" / "mms"
    foundation_dir = base_dir / "data" / "foundation"
    default_checkpoint = base_dir / ".cache" / "icd11_checkpoint.json"
    checkpoint_file = checkpoint_path or default_checkpoint

    # Load checkpoint
    checkpoint = load_checkpoint(checkpoint_file)
    processed_uris = set(checkpoint.get("processed_entity_uris", []))

    if resume:
        print(f"Resuming from checkpoint: {len(processed_uris)} entities already processed")
    else:
        # Clear checkpoint if not resuming
        processed_uris = set()
        checkpoint = {"processed_entity_uris": [], "last_entity_uri": None, "updated_at": ""}

    # Initialize client
    rate_limiter = RateLimiter(5.0)
    client = ICD11Client(client_id, client_secret, rate_limiter)

    # Setup deadline
    started_at = datetime.now(timezone.utc)
    deadline = started_at + timedelta(hours=timeout_hours)

    entities_written = 0
    entities_skipped = 0
    errors: list[str] = []

    try:
        # Get root MMS entity
        print("Fetching MMS root...")
        root = client.get_mms_root()
        root_uri = root.get("@id", "")

        if not root_uri:
            print("Error: Could not get MMS root URI", file=sys.stderr)
            sys.exit(1)

        # Process root if needed
        if root_uri not in processed_uris:
            print(f"Processing root: {root_uri}")
            process_entity(
                client,
                root_uri,
                mms_dir,
                foundation_dir,
                processed_uris,
                checkpoint,
                checkpoint_file,
                errors,
            )
            entities_written += 1
        else:
            entities_skipped += 1

        # Get children of root
        children = client.get_children(root_uri)
        print(f"Found {len(children)} child entities at root level")

        for i, child in enumerate(children):
            if datetime.now(timezone.utc) >= deadline:
                print(f"\nTimeout reached after {timeout_hours} hours. Saving checkpoint.")
                break

            if limit and entities_written >= limit:
                print(f"\nLimit of {limit} entities reached.")
                break

            child_uri = child.get("@id", "")
            if not child_uri:
                continue

            if child_uri in processed_uris:
                entities_skipped += 1
                continue

            print(f"[{i+1}/{len(children)}] Processing: {child_uri}")
            success = process_entity(
                client,
                child_uri,
                mms_dir,
                foundation_dir,
                processed_uris,
                checkpoint,
                checkpoint_file,
                errors,
            )
            if success:
                entities_written += 1
            else:
                entities_skipped += 1

    except httpx.HTTPError as e:
        print(f"HTTP error: {e}", file=sys.stderr)
        errors.append(str(e))
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr)
        errors.append(str(e))
    finally:
        # Save final checkpoint
        save_checkpoint(checkpoint_file, checkpoint)
        print(f"\nCheckpoint saved to {checkpoint_file}")

    # Validate repository after writing
    if entities_written > 0:
        print("\nValidating repository...")
        validation_errors = validate_repository(base_dir)
        if validation_errors:
            print("Validation failed after fetch:", file=sys.stderr)
            for err in validation_errors[:10]:  # Show first 10 errors
                print(f"  - {err}", file=sys.stderr)
            if len(validation_errors) > 10:
                print(f"  ... and {len(validation_errors) - 10} more errors", file=sys.stderr)
            sys.exit(1)
        print("Repository validation passed.")

    if errors:
        print(f"\nCompleted with {len(errors)} error(s)", file=sys.stderr)
        for err in errors[:5]:
            print(f"  - {err}", file=sys.stderr)

    print(f"\nSummary: {entities_written} written, {entities_skipped} skipped")
    return entities_written, entities_skipped


def process_entity(
    client: ICD11Client,
    entity_uri: str,
    mms_dir: Path,
    foundation_dir: Path,
    processed_uris: set[str],
    checkpoint: dict[str, Any],
    checkpoint_path: Path,
    errors: list[str],
) -> bool:
    """Process a single entity and save to appropriate location."""
    try:
        entity = client.get_entity(entity_uri)
    except Exception as e:
        errors.append(f"Failed to fetch {entity_uri}: {e}")
        return False

    # Determine entity type and process accordingly
    entity_type = entity.get("@type", "")
    code = entity.get("code", "")

    # For MMS entities (diseases)
    if "MmsEntity" in str(entity_type) or code:
        # Normalize to disease format
        disease_data = normalize_who_entity_to_disease(entity)

        # Check for existing file to preserve enriched fields
        disease_file = mms_dir / f"{code}.yaml"
        existing = load_yaml(disease_file)
        if existing:
            disease_data = merge_existing_fields(disease_data, existing)

        # Save atomically
        save_yaml_atomic(disease_file, disease_data)
        print(f"  Saved disease: {disease_file.name}")

    # Check for foundation entities linked to this disease
    # Look for terms, manifestations, etc.
    foundation_entities = entity.get("hasPart", []) or entity.get("terms", [])
    for found_entity in foundation_entities:
        if not isinstance(found_entity, dict):
            continue
        found_uri = found_entity.get("@id", "")
        found_code = found_entity.get("code", "")
        if found_uri and found_code:
            # Try to fetch and normalize as symptom
            try:
                foundation_data = client.get_entity(found_uri)
                symptom_data = normalize_who_foundation_entity(foundation_data, found_code)

                # Check for existing file
                symptom_file = foundation_dir / f"{found_code}.yaml"
                existing_symptom = load_yaml(symptom_file)
                if existing_symptom:
                    # Preserve existing fields
                    for key in ["definition_en", "related_systems"]:
                        if key in existing_symptom:
                            symptom_data[key] = existing_symptom[key]

                save_yaml_atomic(symptom_file, symptom_data)
                print(f"  Saved symptom: {symptom_file.name}")
            except Exception:
                pass  # Skip foundation entities that can't be fetched

    # Update checkpoint
    processed_uris.add(entity_uri)
    checkpoint["processed_entity_uris"] = sorted(list(processed_uris))
    checkpoint["last_entity_uri"] = entity_uri
    save_checkpoint(checkpoint_path, checkpoint)

    return True


def main() -> int:
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Fetch ICD-11 MMS entities from WHO API")
    parser.add_argument("--limit", type=int, help="Limit number of entities to fetch")
    parser.add_argument("--resume", action="store_true", help="Resume from checkpoint")
    parser.add_argument("--checkpoint", type=Path, help="Path to checkpoint file")
    parser.add_argument("--root", type=Path, default=None, help="Repository root directory")
    parser.add_argument("--timeout-hours", type=float, default=6.0, help="Maximum runtime in hours")

    args = parser.parse_args()

    # Determine base directory
    if args.root:
        base_dir = args.root
    else:
        base_dir = Path(__file__).parent.parent

    fetch_and_save_entities(
        base_dir=base_dir,
        limit=args.limit,
        resume=args.resume,
        checkpoint_path=args.checkpoint,
        timeout_hours=args.timeout_hours,
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
