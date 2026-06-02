"""Tests for the validate module."""

from pathlib import Path
from typing import Any

import pytest

# Import validator functions directly
import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from validate import (
    load_schema,
    load_yaml,
    validate_grade_probability,
    validate_disease_symptoms_exist,
)


@pytest.fixture
def disease_schema() -> dict[str, Any]:
    """Load the disease schema."""
    schemas_dir = Path(__file__).parent.parent / "schemas"
    return load_schema(schemas_dir / "disease.schema.json")


@pytest.fixture
def symptom_schema() -> dict[str, Any]:
    """Load the symptom schema."""
    schemas_dir = Path(__file__).parent.parent / "schemas"
    return load_schema(schemas_dir / "symptom.schema.json")


@pytest.fixture
def valid_disease_data() -> dict[str, Any]:
    """Return a valid disease data structure."""
    return {
        "entity_uri": "https://example.com/disease",
        "code": "TEST.01",
        "title_en": "Test Disease",
        "definition_en": "A test disease for validation.",
        "parent_code": "TEST",
        "children_codes": [],
        "pathophysiology_en": "Test pathophysiology.",
        "symptoms": [
            {
                "id": "fever",
                "grade": "ALWAYS",
                "probability": 1.0,
                "note": "Always present",
            }
        ],
        "differential_diagnosis": ["Other disease"],
        "risk_factors": ["Risk factor 1"],
        "drugs": ["Drug 1"],
        "vector_text_en": "Test vector text",
        "stats": {},
        "ai_enriched": False,
        "last_updated": "2024-01-15",
    }


@pytest.fixture
def valid_symptom_data() -> dict[str, Any]:
    """Return a valid symptom data structure."""
    return {
        "id": "test_symptom",
        "title_en": "Test Symptom",
        "definition_en": "A test symptom.",
        "related_systems": ["General"],
    }


class TestValidRepository:
    """Test that valid sample repository passes validation."""

    def test_valid_disease_schema(self, disease_schema, valid_disease_data):
        """Valid disease data should pass schema validation."""
        from jsonschema import Draft7Validator

        validator = Draft7Validator(disease_schema)
        errors = list(validator.iter_errors(valid_disease_data))
        assert len(errors) == 0, f"Unexpected validation errors: {errors}"

    def test_valid_symptom_schema(self, symptom_schema, valid_symptom_data):
        """Valid symptom data should pass schema validation."""
        from jsonschema import Draft7Validator

        validator = Draft7Validator(symptom_schema)
        errors = list(validator.iter_errors(valid_symptom_data))
        assert len(errors) == 0, f"Unexpected validation errors: {errors}"

    def test_sample_files_pass(self):
        """The sample files in the repository should pass validation."""
        base_dir = Path(__file__).parent.parent
        mms_file = base_dir / "data" / "mms" / "1B21.0.yaml"
        foundation_files = list((base_dir / "data" / "foundation").glob("*.yaml"))

        assert mms_file.exists(), "Sample disease file should exist"
        assert len(foundation_files) >= 2, "Should have at least 2 symptom files"

        # Load and verify they can be parsed
        disease_data = load_yaml(mms_file)
        assert disease_data is not None

        for f in foundation_files:
            symptom_data = load_yaml(f)
            assert symptom_data is not None


class TestMissingSymptomId:
    """Test that missing symptom IDs fail validation."""

    def test_missing_symptom_id_fails(self):
        """Disease referencing non-existent symptom should fail."""
        foundation_ids = {"fever", "headache"}
        disease_data = {
            "symptoms": [
                {"id": "nonexistent_symptom", "grade": "COMMON", "probability": 0.5, "note": ""}
            ]
        }

        errors = validate_disease_symptoms_exist(disease_data, foundation_ids, "test.yaml")
        assert len(errors) == 1
        assert "nonexistent_symptom" in errors[0]
        assert "not found" in errors[0]


class TestInvalidGradeProbability:
    """Test that invalid grade/probability pairs fail validation."""

    def test_always_with_wrong_probability(self):
        """ALWAYS grade must have probability 1.0."""
        symptom = {"id": "fever", "grade": "ALWAYS", "probability": 0.5, "note": ""}
        errors = validate_grade_probability(symptom, "test.yaml")
        assert len(errors) == 1
        assert "ALWAYS" in errors[0]

    def test_never_with_wrong_probability(self):
        """NEVER grade must have probability 0.0."""
        symptom = {"id": "fever", "grade": "NEVER", "probability": 0.5, "note": ""}
        errors = validate_grade_probability(symptom, "test.yaml")
        assert len(errors) == 1
        assert "NEVER" in errors[0]

    def test_very_common_out_of_range(self):
        """VERY_COMMON grade requires probability 0.7-0.99."""
        symptom = {"id": "fever", "grade": "VERY_COMMON", "probability": 0.5, "note": ""}
        errors = validate_grade_probability(symptom, "test.yaml")
        assert len(errors) == 1

    def test_valid_grade_probability_pair(self):
        """Valid grade/probability pairs should pass."""
        symptom = {"id": "fever", "grade": "COMMON", "probability": 0.5, "note": ""}
        errors = validate_grade_probability(symptom, "test.yaml")
        assert len(errors) == 0

    def test_always_exact_probability(self):
        """ALWAYS with exact 1.0 probability should pass."""
        symptom = {"id": "fever", "grade": "ALWAYS", "probability": 1.0, "note": ""}
        errors = validate_grade_probability(symptom, "test.yaml")
        assert len(errors) == 0


class TestMissingRequiredField:
    """Test that missing required fields fail validation."""

    def test_disease_missing_entity_uri(self, disease_schema):
        """Disease missing entity_uri should fail."""
        from jsonschema import Draft7Validator

        data = {
            "code": "TEST.01",
            "title_en": "Test",
            "definition_en": "Test",
            "parent_code": None,
            "children_codes": [],
            "pathophysiology_en": "Test",
            "symptoms": [],
            "differential_diagnosis": [],
            "risk_factors": [],
            "drugs": [],
            "vector_text_en": "Test",
            "stats": {},
            "ai_enriched": False,
            "last_updated": "2024-01-15",
        }

        validator = Draft7Validator(disease_schema)
        errors = list(validator.iter_errors(data))
        assert len(errors) >= 1
        assert any("entity_uri" in str(e) for e in errors)

    def test_symptom_missing_id(self, symptom_schema):
        """Symptom missing id should fail."""
        from jsonschema import Draft7Validator

        data = {
            "title_en": "Test",
            "definition_en": "Test",
            "related_systems": [],
        }

        validator = Draft7Validator(symptom_schema)
        errors = list(validator.iter_errors(data))
        assert len(errors) >= 1
        assert any("id" in str(e).lower() for e in errors)

    def test_symptom_missing_title_en(self, symptom_schema):
        """Symptom missing title_en should fail."""
        from jsonschema import Draft7Validator

        data = {
            "id": "test",
            "definition_en": "Test",
            "related_systems": [],
        }

        validator = Draft7Validator(symptom_schema)
        errors = list(validator.iter_errors(data))
        assert len(errors) >= 1
        assert any("title_en" in str(e) for e in errors)
