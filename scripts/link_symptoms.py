#!/usr/bin/env python3
"""
Generate reverse symptom-to-disease index.

Scans all disease YAML files, collects symptom references, validates that
every referenced symptom exists in data/foundation/, and generates a
deterministic reverse symptom-to-disease index.

Usage:
    python scripts/link_symptoms.py --data-dir data --output data/generated/links.yaml
    python scripts/link_symptoms.py --data-dir data --output data/generated/links.yaml --include-empty
"""

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml


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


def build_reverse_index(data_dir: Path, include_empty: bool = False) -> dict[str, Any]:
    """Build the reverse symptom-to-disease index.
    
    Args:
        data_dir: Path to the data directory (containing mms/ and foundation/).
        include_empty: If True, include symptoms with no linked diseases.
        
    Returns:
        Dictionary containing the reverse index.
        
    Raises:
        ValueError: If validation fails (missing symptom, id mismatch, etc.)
    """
    mms_dir = data_dir / "mms"
    foundation_dir = data_dir / "foundation"
    
    # Load foundation symptoms
    foundation_symptoms = load_foundation_symptoms(foundation_dir)
    foundation_ids = set(foundation_symptoms.keys())
    
    # Build reverse index: symptom_id -> list of disease references
    reverse_index: dict[str, list[dict[str, Any]]] = {}
    all_errors: list[str] = []
    
    if mms_dir.exists():
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
    
    # Determine which symptoms to include
    if include_empty:
        # Include all foundation symptoms
        symptom_ids_to_include = sorted(foundation_symptoms.keys())
    else:
        # Only include symptoms with at least one disease link
        symptom_ids_to_include = sorted(reverse_index.keys())
    
    for symptom_id in symptom_ids_to_include:
        symptom_title = foundation_symptoms[symptom_id].get("title_en", "")
        diseases_list = reverse_index.get(symptom_id, [])
        
        # Skip symptoms with no diseases unless include_empty is True
        if not include_empty and not diseases_list:
            continue
        
        symptoms_output[symptom_id] = {
            "title_en": symptom_title,
            "diseases": diseases_list,
        }
    
    return {"symptoms": symptoms_output}


def write_reverse_index(base_dir: Path, index: dict[str, Any]) -> Path:
    """Write the reverse index to the output path if content changed.

    Args:
        base_dir: Base directory containing data/ subdirectory.
        index: The reverse index dictionary.

    Returns:
        Path to the output file.
    """
    output_path = base_dir / "data" / "generated" / "links.yaml"
    
    # Generate YAML content
    yaml_content = yaml.dump(index, default_flow_style=False, sort_keys=False, allow_unicode=True)

    # Check if file exists and has same content
    if output_path.exists():
        with open(output_path, "r", encoding="utf-8") as f:
            existing_content = f.read()
        if existing_content == yaml_content:
            return output_path

    # Write the file
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(yaml_content)

    return output_path


def check_reverse_index(base_dir: Path) -> list[str]:
    """Check if the existing reverse index is up to date.

    Args:
        base_dir: Base directory containing data/ subdirectory.

    Returns:
        List of human-readable errors. Empty list means check passed.
    """
    data_dir = base_dir / "data"
    output_path = base_dir / "data" / "generated" / "links.yaml"
    
    # Check if links.yaml exists
    if not output_path.exists():
        return ["data/generated/links.yaml is missing"]

    # Build expected index
    try:
        expected_index = build_reverse_index(data_dir)
    except ValueError as e:
        return [str(e)]

    # Load existing index
    try:
        existing_index = load_yaml(output_path)
    except Exception as e:
        return [f"data/generated/links.yaml failed to load: {e}"]

    # Compare indices
    if expected_index != existing_index:
        return ["data/generated/links.yaml is stale; run python scripts/link_symptoms.py"]

    return []


def main() -> int:
    """Run the link symptoms script and return exit code."""
    parser = argparse.ArgumentParser(
        description="Generate reverse symptom-to-disease index"
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="Path to data directory containing mms/ and foundation/ (default: ./data)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output path for links.yaml (default: data/generated/links.yaml)",
    )
    parser.add_argument(
        "--include-empty",
        action="store_true",
        help="Include symptoms with no linked diseases",
    )
    
    args = parser.parse_args()
    
    # Determine data directory
    if args.data_dir:
        data_dir = args.data_dir
    else:
        data_dir = Path(__file__).parent.parent / "data"
    
    # Determine output path
    if args.output:
        output_path = args.output
    else:
        output_path = data_dir / "generated" / "links.yaml"
    
    # Build the reverse index
    try:
        index = build_reverse_index(data_dir, include_empty=args.include_empty)
    except ValueError as e:
        print(f"Failed to build index:\n{e}")
        return 1
    
    # Write the output file only if content changed
    if write_reverse_index(output_path, index):
        print(f"Generated {output_path}")
    else:
        print(f"{output_path} is up to date (no changes)")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
