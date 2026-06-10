#!/usr/bin/env python3
"""
Build LanceDB vector database from SQLite state.

This script reads ICD-11 data from sync_state.db, generates embeddings
using fastembed, and stores them in a local LanceDB database.

Usage:
    python scripts/build_vectors.py --data-dir data
"""

import argparse
import sys
from pathlib import Path


def main(data_dir: Path) -> int:
    """Main entry point for building vector database."""
    try:
        from src.vectorizer.builder import build_vector_database
        
        print(f"Building vector database from: {data_dir}")
        build_vector_database(data_dir)
        return 0
        
    except ImportError as e:
        print(f"Import error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error building vector database: {e}", file=sys.stderr)
        return 1


def cli() -> int:
    """CLI entry point with argument parsing."""
    parser = argparse.ArgumentParser(description="Build ICD-11 vector database")
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
