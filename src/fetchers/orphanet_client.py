"""Orphanet (Orphadata) API fetcher for rare disease enrichment.

This module fetches epidemiology and HPO phenotype data from Orphanet
and enriches the local SQLite state database.
"""

import json
import logging
import sys
from pathlib import Path
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_fixed

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# Constants
ORPHADATA_BASE_URL = "https://api.orphadata.com"
BATCH_SIZE = 1000
MAX_RETRIES = 3
RETRY_WAIT_SECONDS = 2


def get_db_path() -> Path:
    """Get the path to the SQLite database file."""
    return Path("data") / "db" / "sync_state.db"


def get_db_connection() -> tuple:
    """Get SQLite database connection."""
    # Import here to avoid circular imports
    from src.core.db import init_db
    
    db_path = get_db_path()
    conn = init_db(db_path)
    return conn, db_path


@retry(
    stop=stop_after_attempt(MAX_RETRIES),
    wait=wait_fixed(RETRY_WAIT_SECONDS),
    reraise=True,
)
def fetch_orphanet_endpoint(
    client: httpx.Client, endpoint: str, param: str
) -> dict[str, Any] | None:
    """Fetch data from an Orphanet API endpoint with retry logic.
    
    Args:
        client: HTTPX client instance
        endpoint: API endpoint path (e.g., '/rd-cross-referencing/icd-11s/{}')
        param: The parameter value (ICD code or OrphaCode)
    
    Returns:
        JSON response as dict, or None if not found (404)
    
    Raises:
        httpx.HTTPStatusError: For non-404 errors after retries
    """
    url = f"{ORPHADATA_BASE_URL}{endpoint.format(param)}"
    logger.debug(f"Fetching: {url}")
    
    response = client.get(url)
    
    if response.status_code == 404:
        logger.debug(f"Not found: {param} at {endpoint}")
        return None
    
    response.raise_for_status()
    
    try:
        return response.json()
    except json.JSONDecodeError:
        logger.warning(f"Invalid JSON response for {param}")
        return None


def fetch_orpha_code(client: httpx.Client, icd_code: str) -> str | None:
    """Fetch OrphaCode for a given ICD-11 code.
    
    Args:
        client: HTTPX client instance
        icd_code: ICD-11 code to look up
    
    Returns:
        OrphaCode string if found, None otherwise
    """
    endpoint = "/rd-cross-referencing/icd-11s/{}"
    try:
        data = fetch_orphanet_endpoint(client, endpoint, icd_code)
        if data is None:
            return None
        
        # Extract OrphaCode from response
        # Response structure may vary; handle common patterns
        if isinstance(data, list) and len(data) > 0:
            item = data[0]
            return item.get("orphaCode") or item.get("OrphaCode") or item.get("orpha_code")
        elif isinstance(data, dict):
            return data.get("orphaCode") or data.get("OrphaCode") or data.get("orpha_code")
        
        return None
    except Exception as e:
        logger.warning(f"Error fetching OrphaCode for {icd_code}: {e}")
        return None


def fetch_epidemiology(client: httpx.Client, orpha_code: str) -> dict[str, Any] | None:
    """Fetch epidemiology data for an OrphaCode.
    
    Args:
        client: HTTPX client instance
        orpha_code: OrphaCode to look up
    
    Returns:
        Epidemiology data dict or None
    """
    endpoint = "/rd-epidemiology/orphacodes/{}"
    try:
        return fetch_orphanet_endpoint(client, endpoint, orpha_code)
    except Exception as e:
        logger.warning(f"Error fetching epidemiology for {orpha_code}: {e}")
        return None


def fetch_phenotypes(client: httpx.Client, orpha_code: str) -> list[dict[str, Any]] | None:
    """Fetch HPO phenotype data for an OrphaCode.
    
    Args:
        client: HTTPX client instance
        orpha_code: OrphaCode to look up
    
    Returns:
        List of phenotype dicts or None
    """
    endpoint = "/rd-phenotypes/orphacodes/{}"
    try:
        data = fetch_orphanet_endpoint(client, endpoint, orpha_code)
        if data is None:
            return None
        
        # Handle different response structures
        if isinstance(data, list):
            return data
        elif isinstance(data, dict):
            # May be wrapped in a key like 'phenotypes' or 'clinicalSigns'
            return data.get("phenotypes") or data.get("clinicalSigns") or [data]
        
        return None
    except Exception as e:
        logger.warning(f"Error fetching phenotypes for {orpha_code}: {e}")
        return None


def has_orphanet_data(raw_data_json: str) -> bool:
    """Check if raw_data already contains orphanet key.
    
    Uses SQLite's json_extract or parses JSON directly.
    """
    if raw_data_json is None:
        return False
    
    try:
        raw_data = json.loads(raw_data_json)
        return "orphanet" in raw_data
    except (json.JSONDecodeError, TypeError):
        return False


def update_node_with_orphanet_data(
    conn, icd_code: str, orphanet_data: dict[str, Any] | None
) -> None:
    """Update a node's raw_data with Orphanet information.
    
    Merges orphanet data into existing raw_data without overwriting other keys.
    """
    from src.core.db import get_node_by_code, insert_or_update_node
    
    # Get current node
    cursor = conn.cursor()
    cursor.execute(
        "SELECT title, description, status, raw_data FROM icd_nodes_state WHERE icd_code = ?",
        (icd_code,),
    )
    row = cursor.fetchone()
    
    if row is None:
        logger.warning(f"Node {icd_code} not found in database")
        return
    
    # Parse existing raw_data
    try:
        raw_data = json.loads(row["raw_data"])
    except (json.JSONDecodeError, TypeError):
        raw_data = {}
    
    # Merge orphanet data
    raw_data["orphanet"] = orphanet_data
    
    # Update the node
    insert_or_update_node(
        conn=conn,
        icd_code=icd_code,
        title=row["title"],
        description=row["description"],
        raw_data=raw_data,
        status=row["status"],
    )
    logger.info(f"Updated {icd_code} with Orphanet data")


def process_pending_nodes(conn) -> int:
    """Process pending nodes that don't have Orphanet data yet.
    
    Returns:
        Number of nodes processed
    """
    cursor = conn.cursor()
    
    # Select up to BATCH_SIZE nodes where status is PENDING or ENRICHED
    # and raw_data does NOT contain 'orphanet' key
    cursor.execute("""
        SELECT icd_code, title, description, status, raw_data 
        FROM icd_nodes_state 
        WHERE status IN ('PENDING', 'ENRICHED')
        LIMIT ?
    """, (BATCH_SIZE,))
    
    rows = cursor.fetchall()
    total_processed = 0
    successful_enrichments = 0
    
    # Create HTTPX client with timeout
    with httpx.Client(timeout=30.0) as client:
        for row in rows:
            icd_code = row["icd_code"]
            
            # Double-check if already processed (safety check)
            if has_orphanet_data(row["raw_data"]):
                logger.debug(f"Skipping {icd_code}: already has Orphanet data")
                continue
            
            logger.info(f"Processing {icd_code} ({total_processed + 1}/{len(rows)})")
            
            try:
                # Step 1: Fetch OrphaCode
                orpha_code = fetch_orpha_code(client, icd_code)
                
                if orpha_code is None:
                    # No OrphaCode found - mark as null to prevent re-processing
                    logger.info(f"No OrphaCode found for {icd_code}")
                    update_node_with_orphanet_data(conn, icd_code, None)
                    total_processed += 1
                    continue
                
                logger.info(f"Found OrphaCode {orpha_code} for {icd_code}")
                
                # Step 2: Fetch Epidemiology
                epidemiology = fetch_epidemiology(client, orpha_code)
                
                # Step 3: Fetch Phenotypes
                phenotypes = fetch_phenotypes(client, orpha_code)
                
                # Combine results
                orphanet_data = {
                    "orphaCode": orpha_code,
                    "epidemiology": epidemiology,
                    "phenotypes": phenotypes,
                }
                
                # Update database
                update_node_with_orphanet_data(conn, icd_code, orphanet_data)
                successful_enrichments += 1
                total_processed += 1
                
            except Exception as e:
                logger.error(f"Unexpected error processing {icd_code}: {e}")
                # Mark as null to prevent infinite retry on next run
                update_node_with_orphanet_data(conn, icd_code, None)
                total_processed += 1
                # Continue processing remaining nodes
    
    logger.info(
        f"Processed {total_processed} nodes, {successful_enrichments} enriched with Orphanet data"
    )
    return total_processed


def main() -> int:
    """Main entry point for Orphanet fetcher.
    
    Returns:
        Exit code (0 for success/partial success, 1 for critical failure)
    """
    logger.info("Starting Orphanet data fetcher...")
    db_path = get_db_path()
    logger.info(f"Database path: {db_path}")
    
    # Check if database exists
    if not db_path.exists():
        logger.error(f"Database not found at {db_path}. Run data sync first.")
        return 1
    
    try:
        conn, db_path = get_db_connection()
        logger.info(f"Connected to database: {db_path}")
        
        # Process pending nodes
        processed_count = process_pending_nodes(conn)
        
        if processed_count == 0:
            logger.info("No pending nodes to process")
        else:
            logger.info(f"Successfully processed {processed_count} nodes")
        
        conn.close()
        logger.info("Orphanet fetcher completed successfully")
        return 0
        
    except Exception as e:
        logger.error(f"Critical error: {e}")
        # Exit gracefully to allow partial state to be committed
        return 0


if __name__ == "__main__":
    sys.exit(main())
