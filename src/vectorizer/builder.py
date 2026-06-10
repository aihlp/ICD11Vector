"""Vectorization module for building LanceDB vector database."""

import json
import os
import zipfile
from pathlib import Path

import lancedb
import pyarrow as pa
from fastembed import TextEmbedding


def get_lancedb_path(data_dir: Path) -> Path:
    """Get the path to the LanceDB store directory."""
    db_dir = data_dir / "lancedb_store"
    db_dir.mkdir(parents=True, exist_ok=True)
    return db_dir


def build_vector_database(
    data_dir: Path,
    model_name: str = "intfloat/multilingual-e5-large",
    batch_size: int = 32,
) -> None:
    """Build LanceDB vector database from SQLite state.
    
    Connects to sync_state.db, selects rows with status 'PENDING' or 'ENRICHED',
    generates embeddings using fastembed, and stores them in LanceDB.
    
    Args:
        data_dir: Base data directory containing db/ and lancedb_store/
        model_name: Name of the embedding model to use
        batch_size: Batch size for embedding generation
    """
    # Import here to avoid circular imports
    from src.core.db import (
        count_nodes_by_status,
        get_db_path,
        get_nodes_by_status,
        init_db,
        update_node_status,
    )
    
    db_path = get_db_path(data_dir)
    lancedb_path = get_lancedb_path(data_dir)
    
    # Initialize and connect to SQLite
    sqlite_conn = init_db(db_path)
    
    # Get nodes that need vectorization (PENDING or ENRICHED status)
    pending_count = count_nodes_by_status(sqlite_conn, "PENDING")
    enriched_count = count_nodes_by_status(sqlite_conn, "ENRICHED")
    
    if pending_count == 0 and enriched_count == 0:
        print("No nodes to vectorize.")
        sqlite_conn.close()
        return
    
    print(f"Found {pending_count} PENDING and {enriched_count} ENRICHED nodes to vectorize.")
    
    # Get all nodes to vectorize
    nodes_to_vectorize = []
    for status in ["PENDING", "ENRICHED"]:
        nodes = get_nodes_by_status(sqlite_conn, status)
        nodes_to_vectorize.extend(nodes)
    
    # Initialize embedding model
    print(f"Loading embedding model: {model_name}")
    embedding_model = TextEmbedding(model_name=model_name)
    
    # Prepare data for LanceDB
    # Schema: icd_code, title, description, vector, metadata_json
    schema = pa.schema([
        pa.field("icd_code", pa.string()),
        pa.field("title", pa.string()),
        pa.field("description", pa.string()),
        pa.field("vector", pa.list_(pa.float32())),
        pa.field("metadata_json", pa.string()),
    ])
    
    # Connect to LanceDB
    print(f"Connecting to LanceDB at: {lancedb_path}")
    db = lancedb.connect(str(lancedb_path))
    
    # Create or open table
    table_name = "icd11_vectors"
    try:
        table = db.open_table(table_name)
        print(f"Opened existing table: {table_name}")
    except Exception:
        table = db.create_table(table_name, schema=schema)
        print(f"Created new table: {table_name}")
    
    # Process nodes in batches
    total_processed = 0
    for i in range(0, len(nodes_to_vectorize), batch_size):
        batch = nodes_to_vectorize[i:i + batch_size]
        
        # Prepare texts for embedding (title + description)
        texts = []
        icd_codes = []
        titles = []
        descriptions = []
        metadata_list = []
        
        for node in batch:
            raw_data = json.loads(node["raw_data"])
            title = node["title"]
            description = node["description"]
            
            # Combine title and description for embedding
            text_for_embedding = f"{title} {description}".strip()
            if not text_for_embedding:
                text_for_embedding = title  # Fallback to title only
            
            texts.append(text_for_embedding)
            icd_codes.append(node["icd_code"])
            titles.append(title)
            descriptions.append(description)
            metadata_list.append(json.dumps({
                "raw_data": raw_data,
                "source": "icd11_who_api",
            }))
        
        # Generate embeddings
        embeddings = list(embedding_model.embed(texts))
        
        # Prepare batch data for LanceDB
        batch_data = {
            "icd_code": icd_codes,
            "title": titles,
            "description": descriptions,
            "vector": [emb.tolist() for emb in embeddings],
            "metadata_json": metadata_list,
        }
        
        # Add to LanceDB table
        table.add(batch_data)
        
        # Update SQLite status to VECTORIZED
        for icd_code in icd_codes:
            update_node_status(sqlite_conn, icd_code, "VECTORIZED")
        
        total_processed += len(batch)
        print(f"Processed batch {i // batch_size + 1}: {len(batch)} nodes ({total_processed}/{len(nodes_to_vectorize)})")
    
    sqlite_conn.close()
    print(f"Vectorization complete. Total nodes processed: {total_processed}")
    print(f"LanceDB store location: {lancedb_path}")


def export_vector_database(data_dir: Path, output_path: Path) -> None:
    """Export LanceDB database to a zip file for GitHub Release.
    
    Args:
        data_dir: Base data directory containing lancedb_store/
        output_path: Path for the output zip file
    """
    lancedb_path = get_lancedb_path(data_dir)
    
    if not lancedb_path.exists():
        raise FileNotFoundError(f"LanceDB store not found at {lancedb_path}")
    
    # Create zip file
    print(f"Creating zip archive: {output_path}")
    with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for root, dirs, files in os.walk(lancedb_path):
            for file in files:
                file_path = Path(root) / file
                arcname = file_path.relative_to(lancedb_path.parent)
                zipf.write(file_path, arcname)
    
    print(f"Export complete: {output_path}")
