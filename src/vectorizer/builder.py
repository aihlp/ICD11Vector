"""Vectorization module for building LanceDB vector database from YAML files."""

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


def load_yaml(file_path: Path) -> dict | None:
    """Load a YAML file."""
    try:
        import yaml
        with open(file_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)  # type: ignore[no-any-return]
    except Exception:
        return None


def build_vector_database(
    data_dir: Path,
    model_name: str = "intfloat/multilingual-e5-large",
    batch_size: int = 32,
) -> None:
    """Build LanceDB vector database from enriched YAML files.
    
    Reads all YAML files from data/mms/, generates embeddings for
    vector_text_en field using fastembed, and stores them in LanceDB.
    
    Args:
        data_dir: Base data directory containing mms/ and lancedb_store/
        model_name: Name of the embedding model to use
        batch_size: Batch size for embedding generation
    """
    mms_dir = data_dir / "mms"
    lancedb_path = get_lancedb_path(data_dir)
    
    if not mms_dir.exists():
        print(f"MMS directory not found: {mms_dir}")
        return
    
    # Get all YAML files
    yaml_files = list(mms_dir.glob("*.yaml"))
    
    if not yaml_files:
        print("No YAML files found to vectorize.")
        return
    
    # Filter to only AI-enriched files (have vector_text_en)
    files_to_vectorize = []
    for yaml_file in yaml_files:
        data = load_yaml(yaml_file)
        if data and data.get("ai_enriched", False) and data.get("vector_text_en"):
            files_to_vectorize.append((yaml_file, data))
    
    if not files_to_vectorize:
        print("No AI-enriched files with vector_text_en found to vectorize.")
        print("Run LLM enrichment first to generate vector_text_en content.")
        return
    
    print(f"Found {len(files_to_vectorize)} AI-enriched files to vectorize.")
    
    # Initialize embedding model
    print(f"Loading embedding model: {model_name}")
    embedding_model = TextEmbedding(model_name=model_name)
    
    # Prepare data for LanceDB
    # Schema: icd_code, title, vector_text, vector, metadata_json
    schema = pa.schema([
        pa.field("icd_code", pa.string()),
        pa.field("title", pa.string()),
        pa.field("vector_text", pa.string()),
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
    
    # Process files in batches
    total_processed = 0
    for i in range(0, len(files_to_vectorize), batch_size):
        batch = files_to_vectorize[i:i + batch_size]
        
        # Prepare texts for embedding
        texts = []
        icd_codes = []
        titles = []
        vector_texts = []
        metadata_list = []
        
        for yaml_file, data in batch:
            icd_code = data.get("code", "")
            title = data.get("title_en", "")
            vector_text = data.get("vector_text_en", "")
            
            if not vector_text:
                continue
            
            texts.append(vector_text)
            icd_codes.append(icd_code)
            titles.append(title)
            vector_texts.append(vector_text)
            metadata_list.append(__import__('json').dumps({
                "entity_uri": data.get("entity_uri", ""),
                "definition_en": data.get("definition_en", ""),
                "ai_enriched": data.get("ai_enriched", False),
                "symptoms_count": len(data.get("symptoms", [])),
            }))
        
        if not texts:
            continue
        
        # Generate embeddings
        embeddings = list(embedding_model.embed(texts))
        
        # Prepare batch data for LanceDB
        batch_data = {
            "icd_code": icd_codes,
            "title": titles,
            "vector_text": vector_texts,
            "vector": [emb.tolist() for emb in embeddings],
            "metadata_json": metadata_list,
        }
        
        # Add to LanceDB table
        table.add(batch_data)
        
        total_processed += len(batch)
        print(f"Processed batch {i // batch_size + 1}: {len(batch)} files ({total_processed}/{len(files_to_vectorize)})")
    
    print(f"Vectorization complete. Total files processed: {total_processed}")
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
