#!/usr/bin/env python3
"""
Generate reverse symptom-to-disease index.

Scans all disease YAML files, collects symptom references, validates that
every referenced symptom exists in data/foundation/, and generates a
deterministic reverse symptom-to-disease index.

Usage:
    python scripts/link_symptoms.py [--check] [--root PATH]
"""

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]


def load_yaml(file_path: Path) -> dict[str, Any]:
    """Load a YAML file."""
    with open(file_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)  # type: ignore[no-any-return]


def load_foundation_symptoms(foundation_dir: Path) -> dict[str, dict[str, Any]]:
    """Load all foundation symptom YAML files.
    
    Returns:
        Dictionary mapping symptom id to symptom data.
        
    Raises:
        ValueError: If symptom id doesn't match filename stem.
    """
    symptoms: dict[str, dict[str, Any]] = {}
    
    if not foundation_dir.exists():
        return symptoms
    
    for yaml_file in sorted(foundation_dir.glob("*.yaml")):
        data = load_yaml(yaml_file)
        symptom_id = data.get("id")
        filename_stem = yaml_file.stem
        
        if symptom_id != filename_stem:
            raise ValueError(
                f"Symptom id '{symptom_id}' does not match filename '{yaml_file.name}'"
            )
        
        symptoms[symptom_id] = data
    
    return symptoms


def validate_disease(disease_data: dict[str, Any], disease_file: Path) -> list[str]:
    """Validate a disease YAML file has required fields.
    
    Returns:
        List of error messages.
    """
    errors: list[str] = []
    
    code = disease_data.get("code")
    if not code:
        errors.append(f"{disease_file}: Missing or empty 'code'")
    
    title_en = disease_data.get("title_en")
    if not title_en:
        errors.append(f"{disease_file}: Missing or empty 'title_en'")
    
    # Validate symptoms have id
    for symptom in disease_data.get("symptoms", []):
        if "id" not in symptom:
            errors.append(f"{disease_file}: Symptom reference missing 'id'")
    
    return errors


def build_reverse_index(base_dir: Path) -> dict[str, Any]:
    """Build the reverse symptom-to-disease index.
    
    Args:
        base_dir: Path to the repository root directory.
        
    Returns:
        Dictionary containing the reverse index.
        
    Raises:
        ValueError: If validation fails (missing symptom, id mismatch, etc.)
    """
    mms_dir = base_dir / "data" / "mms"
    foundation_dir = base_dir / "data" / "foundation"
    
    # Load foundation symptoms
    foundation_symptoms = load_foundation_symptoms(foundation_dir)
    foundation_ids = set(foundation_symptoms.keys())
    
    # Build reverse index: symptom_id -> list of disease references
    reverse_index: dict[str, list[dict[str, Any]]] = {}
    all_errors: list[str] = []
    
    if not mms_dir.exists():
        return {"symptoms": {}}
    
    for yaml_file in sorted(mms_dir.glob("*.yaml")):
        try:
            disease_data = load_yaml(yaml_file)
        except Exception as e:
            all_errors.append(f"{yaml_file}: Failed to load YAML: {e}")
            continue
        
        # Validate disease has required fields
        all_errors.extend(validate_disease(disease_data, yaml_file))
        
        # Process symptoms
        for symptom_ref in disease_data.get("symptoms", []):
            symptom_id = symptom_ref.get("id")
            
            if not symptom_id:
                continue  # Already reported as error
            
            # Check symptom exists in foundation
            if symptom_id not in foundation_ids:
                all_errors.append(
                    f"{yaml_file}: Referenced symptom '{symptom_id}' not found in data/foundation/"
                )
                continue
            
            # Get symptom title from foundation
            symptom_title = foundation_symptoms[symptom_id].get("title_en", "")
            
            # Build disease link entry
            disease_link = {
                "code": disease_data.get("code", ""),
                "title_en": disease_data.get("title_en", ""),
                "grade": symptom_ref.get("grade"),
                "probability": symptom_ref.get("probability"),
                "note": symptom_ref.get("note"),
            }
            
            if symptom_id not in reverse_index:
                reverse_index[symptom_id] = []
            
            reverse_index[symptom_id].append(disease_link)
    
    if all_errors:
        raise ValueError("\n".join(all_errors))
    
    # Sort disease links by code for each symptom
    for symptom_id in reverse_index:
        reverse_index[symptom_id].sort(key=lambda x: x.get("code", ""))
    
    # Build final output structure with symptoms sorted alphabetically
    symptoms_output: dict[str, dict[str, Any]] = {}
    for symptom_id in sorted(reverse_index.keys()):
        symptom_title = foundation_symptoms[symptom_id].get("title_en", "")
        symptoms_output[symptom_id] = {
            "title_en": symptom_title,
            "diseases": reverse_index[symptom_id],
        }
    
    return {"symptoms": symptoms_output}


def write_reverse_index(base_dir: Path, index: dict[str, Any]) -> Path:
    """Write the reverse index to data/generated/links.yaml.
    
    Args:
        base_dir: Path to the repository root directory.
        index: The reverse index dictionary.
        
    Returns:
        Path to the written file.
    """
    generated_dir = base_dir / "data" / "generated"
    generated_dir.mkdir(parents=True, exist_ok=True)
    
    output_file = generated_dir / "links.yaml"
    
    with open(output_file, "w", encoding="utf-8") as f:
        yaml.dump(index, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
    
    return output_file


def check_reverse_index(base_dir: Path) -> list[str]:
    """Check if the existing reverse index is up to date.
    
    Args:
        base_dir: Path to the repository root directory.
        
    Returns:
        List of human-readable errors. Empty list means check passed.
    """
    generated_file = base_dir / "data" / "generated" / "links.yaml"
    
    # Check if links.yaml exists
    if not generated_file.exists():
        return ["data/generated/links.yaml is missing"]
    
    # Build expected index
    try:
        expected_index = build_reverse_index(base_dir)
    except ValueError as e:
        return [str(e)]
    
    # Load existing index
    try:
        existing_index = load_yaml(generated_file)
    except Exception as e:
        return [f"data/generated/links.yaml failed to load: {e}"]
    
    # Compare indices
    if expected_index != existing_index:
        return ["data/generated/links.yaml is stale (differs from expected output)"]
    
    return []


def main() -> int:
    """Run the link symptoms script and return exit code."""
    parser = argparse.ArgumentParser(
        description="Generate or check reverse symptom-to-disease index"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check if existing links.yaml is up to date instead of generating",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Repository root directory (default: parent of script)",
    )
    
    args = parser.parse_args()
    
    # Determine base directory
    if args.root:
        base_dir = args.root
    else:
        base_dir = Path(__file__).parent.parent
    
    if args.check:
        # Check mode
        errors = check_reverse_index(base_dir)
        
        if errors:
            print(f"Check failed with {len(errors)} error(s):\n")
            for i, error in enumerate(errors, 1):
                print(f"{i}. {error}")
            return 1
        
        print("Reverse index is up to date.")
        return 0
    else:
        # Generate mode
        try:
            index = build_reverse_index(base_dir)
        except ValueError as e:
            print(f"Failed to build index:\n{e}")
            return 1
        
        output_file = write_reverse_index(base_dir, index)
        print(f"Generated {output_file}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
