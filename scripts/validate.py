#!/usr/bin/env python3
"""
Validate disease and symptom YAML files against JSON schemas.

Usage:
    python scripts/validate.py [base_dir]
"""

import json
import sys
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft7Validator


def load_yaml(file_path: Path) -> dict[str, Any]:
    """Load a YAML file."""
    with open(file_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)  # type: ignore[no-any-return]


# Grade to probability range mapping (stricter policy)
GRADE_PROBABILITY_RANGES = {
    "ALWAYS": (1.0, 1.0),
    "VERY_COMMON": (0.7, 0.99),
    "COMMON": (0.3, 0.69),
    "OCCASIONAL": (0.05, 0.29),
    "RARE": (0.001, 0.049),
    "NEVER": (0.0, 0.0),
}


def load_schema(schema_path: Path) -> dict[str, Any]:
    """Load a JSON schema from file."""
    with open(schema_path, "r", encoding="utf-8") as f:
        return json.load(f)  # type: ignore[no-any-return]


def validate_grade_probability(symptom: dict[str, Any], disease_file: str) -> list[str]:
    """Validate that grade and probability are consistent."""
    errors: list[str] = []
    grade = symptom.get("grade")
    probability = symptom.get("probability")
    symptom_id = symptom.get("id", "<unknown>")

    if grade not in GRADE_PROBABILITY_RANGES:
        errors.append(
            f"{disease_file}: Symptom '{symptom_id}' has invalid grade '{grade}'"
        )
        return errors

    min_prob, max_prob = GRADE_PROBABILITY_RANGES[grade]

    if probability is None:
        errors.append(
            f"{disease_file}: Symptom '{symptom_id}' missing probability value"
        )
        return errors

    # Use small epsilon for floating point comparison
    epsilon = 0.0001
    if not (min_prob - epsilon <= probability <= max_prob + epsilon):
        errors.append(
            f"{disease_file}: Symptom '{symptom_id}' has probability {probability} "
            f"but grade '{grade}' requires range [{min_prob}, {max_prob}]"
        )

    return errors


def validate_disease_symptoms_exist(
    disease_data: dict[str, Any],
    foundation_ids: set[str],
    disease_file: str,
) -> list[str]:
    """Validate that all symptom IDs referenced in disease exist in foundation."""
    errors: list[str] = []
    symptoms = disease_data.get("symptoms", [])

    for symptom in symptoms:
        symptom_id = symptom.get("id")
        if symptom_id and symptom_id not in foundation_ids:
            errors.append(
                f"{disease_file}: Symptom id '{symptom_id}' not found in data/foundation/"
            )

    return errors


def validate_directory(
    data_dir: Path, schema: dict[str, Any], schema_name: str
) -> tuple[list[dict[str, Any]], set[str]]:
    """Validate all YAML files in a directory against a schema."""
    errors: list[dict[str, Any]] = []
    ids: set[str] = set()

    validator = Draft7Validator(schema)

    if not data_dir.exists():
        return errors, ids

    for yaml_file in sorted(data_dir.glob("*.yaml")):
        try:
            data = load_yaml(yaml_file)
        except Exception as e:
            errors.append({"file": str(yaml_file), "error": f"Failed to load YAML: {e}"})
            continue

        # Validate against JSON schema
        for error in validator.iter_errors(data):
            errors.append(
                {
                    "file": str(yaml_file),
                    "error": f"Schema validation: {'.'.join(str(p) for p in error.path)} - {error.message}",
                }
            )

        # Collect ID for cross-reference validation
        if "id" in data:
            ids.add(data["id"])

    return errors, ids


def validate_repository(base_dir: Path) -> list[str]:
    """Validate the entire repository structure.
    
    Args:
        base_dir: Path to the repository root directory.
        
    Returns:
        List of error messages. Empty list means validation passed.
    """
    schemas_dir = base_dir / "schemas"
    mms_dir = base_dir / "data" / "mms"
    foundation_dir = base_dir / "data" / "foundation"

    all_errors: list[Any] = []

    # Load schemas
    try:
        disease_schema = load_schema(schemas_dir / "disease.schema.json")
    except Exception as e:
        return [f"Error loading disease schema: {e}"]

    try:
        symptom_schema = load_schema(schemas_dir / "symptom.schema.json")
    except Exception as e:
        return [f"Error loading symptom schema: {e}"]

    # Validate foundation symptoms first to get valid IDs
    foundation_errors, foundation_ids = validate_directory(
        foundation_dir, symptom_schema, "symptom"
    )
    all_errors.extend(foundation_errors)

    # Validate diseases
    disease_errors, _ = validate_directory(mms_dir, disease_schema, "disease")

    # Additional semantic validation for diseases
    for yaml_file in sorted(mms_dir.glob("*.yaml")) if mms_dir.exists() else []:
        try:
            disease_data = load_yaml(yaml_file)
        except Exception:
            continue  # Already reported as error

        # Validate symptom IDs exist
        all_errors.extend(
            validate_disease_symptoms_exist(
                disease_data, foundation_ids, str(yaml_file)
            )
        )

        # Validate grade/probability consistency
        for symptom in disease_data.get("symptoms", []):
            all_errors.extend(validate_grade_probability(symptom, str(yaml_file)))

    all_errors.extend(disease_errors)

    # Convert all errors to strings
    string_errors: list[str] = []
    for error in all_errors:
        if isinstance(error, dict):
            string_errors.append(f"[{error.get('file', 'unknown')}] {error.get('error', error)}")
        else:
            string_errors.append(str(error))

    return string_errors


def main() -> int:
    """Run validation and return exit code."""
    # Accept optional base_dir argument, default to parent of script
    if len(sys.argv) > 1:
        base_dir = Path(sys.argv[1])
    else:
        base_dir = Path(__file__).parent.parent
    
    data_dir = base_dir / "data"
    
    all_errors = validate_repository(base_dir)
    
    # Check if links.yaml exists and is up to date
    links_file = data_dir / "generated" / "links.yaml"
    if links_file.exists():
        # Import locally to avoid mypy issues with scripts directory
        import importlib.util
        link_symptoms_path = Path(__file__).parent / "link_symptoms.py"
        spec = importlib.util.spec_from_file_location("link_symptoms", link_symptoms_path)
        if spec and spec.loader:
            link_symptoms = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(link_symptoms)
            
            build_reverse_index = link_symptoms.build_reverse_index
            load_yaml = link_symptoms.load_yaml
            
            try:
                expected_index = build_reverse_index(base_dir)
            except ValueError as e:
                all_errors.append(str(e))
            
            try:
                existing_index = load_yaml(links_file)
            except Exception as e:
                all_errors.append(f"data/generated/links.yaml failed to load: {e}")
            
            if expected_index != existing_index:
                all_errors.append("data/generated/links.yaml is stale; run python scripts/link_symptoms.py")

    # Report results
    if all_errors:
        print(f"Validation failed with {len(all_errors)} error(s):\n")
        for i, error in enumerate(all_errors, 1):
            print(f"{i}. {error}")
        return 1

    print("Validation passed successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
