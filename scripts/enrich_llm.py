#!/usr/bin/env python3
"""
LLM-based enrichment for YAML files.

This script uses an LLM (OpenAI) to generate:
- pathophysiology_en
- differential_diagnosis
- drugs
- vector_text_en (comprehensive text for embeddings)

It processes only a small batch of N cards per run to avoid API rate limits
and GitHub Actions timeouts. Only processes files where:
- externally_enriched: true
- ai_enriched: false

Usage:
    python scripts/enrich_llm.py --data-dir data --batch-size 20

Environment variables:
    OPENAI_API_KEY: OpenAI API key for LLM access
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml


def load_yaml(file_path: Path) -> dict[str, Any] | None:
    """Load a YAML file."""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)  # type: ignore[no-any-return]
    except Exception as e:
        print(f"Error loading {file_path}: {e}")
        return None


def save_yaml(file_path: Path, data: dict[str, Any]) -> None:
    """Save data to a YAML file."""
    with open(file_path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)


def build_llm_prompt(disease_data: dict[str, Any]) -> str:
    """Build the prompt for LLM enrichment."""
    title = disease_data.get("title_en", "")
    definition = disease_data.get("definition_en", "")
    symptoms = disease_data.get("symptoms", [])
    risk_factors = disease_data.get("risk_factors", [])
    
    symptom_list = ", ".join([s.get("id", "") for s in symptoms]) if symptoms else "None specified"
    risk_factor_list = ", ".join(risk_factors[:5]) if risk_factors else "None specified"
    
    prompt = f"""You are a medical knowledge expert. Generate comprehensive information for the following ICD-11 disease entity:

**Disease Information:**
- Title: {title}
- Definition: {definition}
- Known Symptoms: {symptom_list}
- Known Risk Factors: {risk_factor_list}

Please provide the following in JSON format:

1. **pathophysiology_en**: A detailed description of the disease mechanism (2-4 sentences). Explain how the disease develops and progresses at a physiological level.

2. **differential_diagnosis**: An array of 3-5 related conditions that should be considered in differential diagnosis. These should be medically relevant conditions with similar presentations.

3. **drugs**: An array of 3-7 medications commonly used to treat this condition. Include both generic drug names.

4. **vector_text_en**: A comprehensive text (150-300 words) optimized for semantic search/vector embeddings. This should combine:
   - The disease title and definition
   - Key symptoms and clinical presentation
   - Pathophysiology summary
   - Relevant risk factors
   - Treatment context
   
   Write this as natural, flowing text that would match various search queries about this condition.

Respond ONLY with valid JSON in this exact format:
{{
    "pathophysiology_en": "...",
    "differential_diagnosis": ["condition1", "condition2", ...],
    "drugs": ["drug1", "drug2", ...],
    "vector_text_en": "..."
}}
"""
    return prompt


def call_llm_api(prompt: str) -> dict[str, Any] | None:
    """Call the OpenAI API to get LLM enrichment."""
    api_key = os.environ.get("OPENAI_API_KEY")
    
    if not api_key:
        print("Error: OPENAI_API_KEY environment variable not set")
        return None
    
    try:
        from openai import OpenAI
        
        client = OpenAI(api_key=api_key)
        
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": "You are a medical knowledge assistant. Provide accurate, concise information in JSON format."
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            temperature=0.3,
            max_tokens=1000,
            response_format={"type": "json_object"}
        )
        
        content = response.choices[0].message.content
        if content:
            return json.loads(content)  # type: ignore[no-any-return]
        return None
        
    except ImportError:
        print("Error: openai package not installed. Install with: pip install openai")
        return None
    except Exception as e:
        print(f"LLM API error: {e}")
        return None


def enrich_yaml_with_llm(
    yaml_path: Path, 
    llm_result: dict[str, Any]
) -> bool:
    """Apply LLM enrichment to a YAML file."""
    data = load_yaml(yaml_path)
    if not data:
        return False
    
    # Apply LLM-generated content
    if "pathophysiology_en" in llm_result:
        data["pathophysiology_en"] = llm_result["pathophysiology_en"]
    
    if "differential_diagnosis" in llm_result:
        data["differential_diagnosis"] = llm_result["differential_diagnosis"]
    
    if "drugs" in llm_result:
        data["drugs"] = llm_result["drugs"]
    
    if "vector_text_en" in llm_result:
        data["vector_text_en"] = llm_result["vector_text_en"]
    
    # Mark as AI enriched
    data["ai_enriched"] = True
    data["last_updated"] = datetime.utcnow().strftime("%Y-%m-%d")
    
    # Save updated YAML
    save_yaml(yaml_path, data)
    return True


def get_files_needing_enrichment(mms_dir: Path) -> list[Path]:
    """Get list of YAML files that need LLM enrichment."""
    files = []
    
    for yaml_file in mms_dir.glob("*.yaml"):
        data = load_yaml(yaml_file)
        if not data:
            continue
        
        # Check enrichment flags
        externally_enriched = data.get("externally_enriched", False)
        ai_enriched = data.get("ai_enriched", False)
        
        # Only process if externally enriched but not AI enriched
        if externally_enriched and not ai_enriched:
            files.append(yaml_file)
    
    return sorted(files)


def main(data_dir: Path, batch_size: int = 20) -> int:
    """Main entry point for LLM enrichment."""
    mms_dir = data_dir / "mms"
    
    if not mms_dir.exists():
        print(f"MMS directory not found: {mms_dir}")
        return 1
    
    # Check API key
    if not os.environ.get("OPENAI_API_KEY"):
        print("Warning: OPENAI_API_KEY not set. LLM enrichment will be skipped.")
        print("Set the environment variable to enable AI enrichment.")
        return 0
    
    # Get files needing enrichment
    files_to_enrich = get_files_needing_enrichment(mms_dir)
    
    if not files_to_enrich:
        print("No files need LLM enrichment (all externally enriched files are already AI enriched)")
        return 0
    
    print(f"Found {len(files_to_enrich)} files needing LLM enrichment")
    print(f"Processing batch of up to {batch_size} files")
    
    # Limit to batch size
    batch = files_to_enrich[:batch_size]
    remaining = len(files_to_enrich) - batch_size
    
    enriched_count = 0
    failed_count = 0
    skipped_count = 0
    
    for i, yaml_file in enumerate(batch, 1):
        print(f"\n[{i}/{len(batch)}] Processing {yaml_file.name}...")
        
        data = load_yaml(yaml_file)
        if not data:
            print("  Skipped (failed to load)")
            skipped_count += 1
            continue
        
        # Build prompt
        prompt = build_llm_prompt(data)
        
        # Call LLM API
        llm_result = call_llm_api(prompt)
        
        if llm_result is None:
            print("  Failed (LLM API error)")
            failed_count += 1
            # Continue with next file - don't abort on single failure
            time.sleep(1)  # Brief pause before retry
            continue
        
        # Apply enrichment
        if enrich_yaml_with_llm(yaml_file, llm_result):
            print("  Enriched successfully")
            enriched_count += 1
            
            # Rate limiting - pause between requests
            time.sleep(0.5)
        else:
            print("  Failed to apply enrichment")
            failed_count += 1
    
    print(f"\n{'='*50}")
    print("LLM Enrichment Batch Complete:")
    print(f"  Processed: {enriched_count + failed_count + skipped_count}")
    print(f"  Enriched: {enriched_count}")
    print(f"  Failed: {failed_count}")
    print(f"  Skipped: {skipped_count}")
    
    if remaining > 0:
        print(f"\n  Remaining files: {remaining}")
        print("  Next scheduled run will continue processing")
    
    return 0


def cli() -> int:
    """CLI entry point with argument parsing."""
    parser = argparse.ArgumentParser(description="LLM-based YAML enrichment (batch processing)")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(__file__).parent.parent / "data",
        help="Data directory (default: ../data)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=20,
        help="Number of files to process in this batch (default: 20)",
    )
    
    args = parser.parse_args()
    return main(args.data_dir, args.batch_size)


if __name__ == "__main__":
    sys.exit(cli())
