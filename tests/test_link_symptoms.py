"""Tests for the link_symptoms module."""

from pathlib import Path

import pytest
import yaml  # type: ignore[import-untyped]

# Import functions from link_symptoms
import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from link_symptoms import (  # type: ignore[import-not-found]
    build_reverse_index,
    write_reverse_index,
    check_reverse_index,
)


@pytest.fixture
def sample_repo(tmp_path: Path) -> Path:
    """Create a temporary repository with sample data."""
    # Create directory structure
    mms_dir = tmp_path / "data" / "mms"
    foundation_dir = tmp_path / "data" / "foundation"
    generated_dir = tmp_path / "data" / "generated"
    
    mms_dir.mkdir(parents=True)
    foundation_dir.mkdir(parents=True)
    generated_dir.mkdir(parents=True)
    
    # Create foundation symptoms
    fever_data = {
        "id": "fever",
        "title_en": "Fever",
        "definition_en": "Elevated body temperature.",
        "related_systems": ["General"],
    }
    with open(foundation_dir / "fever.yaml", "w") as f:
        yaml.dump(fever_data, f)
    
    headache_data = {
        "id": "headache",
        "title_en": "Headache",
        "definition_en": "Pain in the head.",
        "related_systems": ["Nervous system"],
    }
    with open(foundation_dir / "headache.yaml", "w") as f:
        yaml.dump(headache_data, f)
    
    # Create disease
    disease_data = {
        "entity_uri": "https://example.com/disease",
        "code": "TEST.01",
        "title_en": "Test Disease",
        "definition_en": "A test disease.",
        "parent_code": "TEST",
        "children_codes": [],
        "pathophysiology_en": "Test pathophysiology.",
        "symptoms": [
            {
                "id": "fever",
                "grade": "ALWAYS",
                "probability": 1.0,
                "note": "Always present",
            },
            {
                "id": "headache",
                "grade": "VERY_COMMON",
                "probability": 0.85,
                "note": "Often severe",
            },
        ],
        "differential_diagnosis": ["Other disease"],
        "risk_factors": ["Risk factor 1"],
        "drugs": ["Drug 1"],
        "vector_text_en": "Test vector text",
        "stats": {
            "mortality_global_annual": None,
            "dalys_global": None,
            "incidence_rate_per_100k": None,
            "active_clinical_trials": None,
            "child_code_count": 0,
            "research_link_count": 0,
        },
        "ai_enriched": False,
        "last_updated": "2024-01-15",
    }
    with open(mms_dir / "TEST.01.yaml", "w") as f:
        yaml.dump(disease_data, f)
    
    return tmp_path


class TestGenerateLinksYaml:
    """Test that link_symptoms.py generates data/generated/links.yaml."""
    
    def test_generates_links_yaml(self, sample_repo: Path):
        """Should generate links.yaml file."""
        index = build_reverse_index(sample_repo)
        output_file = sample_repo / "data" / "generated" / "links.yaml"
        write_reverse_index(output_file, index)
        
        assert output_file.exists()
        assert output_file == sample_repo / "data" / "generated" / "links.yaml"
    
    def test_output_contains_required_fields(self, sample_repo: Path):
        """Output should contain symptom id, title, disease code, title, grade, probability, note."""
        index = build_reverse_index(sample_repo)
        
        symptoms = index.get("symptoms", {})
        assert "fever" in symptoms
        assert "headache" in symptoms
        
        fever_data = symptoms["fever"]
        assert fever_data["title_en"] == "Fever"
        assert len(fever_data["diseases"]) == 1
        
        disease_link = fever_data["diseases"][0]
        assert disease_link["code"] == "TEST.01"
        assert disease_link["title_en"] == "Test Disease"
        assert disease_link["grade"] == "ALWAYS"
        assert disease_link["probability"] == 1.0
        assert disease_link["note"] == "Always present"


class TestDeterministicOrdering:
    """Test that output is deterministically ordered."""
    
    def test_symptoms_sorted_alphabetically(self, sample_repo: Path):
        """Symptom ids should be sorted alphabetically."""
        index = build_reverse_index(sample_repo)
        symptom_ids = list(index["symptoms"].keys())
        
        assert symptom_ids == sorted(symptom_ids)
    
    def test_disease_links_sorted_by_code(self, sample_repo: Path):
        """Disease links should be sorted by code."""
        # Add another disease with lower code
        mms_dir = sample_repo / "data" / "mms"
        disease_data = {
            "entity_uri": "https://example.com/disease2",
            "code": "AAA.00",
            "title_en": "Earlier Disease",
            "definition_en": "An earlier disease.",
            "parent_code": "AAA",
            "children_codes": [],
            "pathophysiology_en": "Test.",
            "symptoms": [
                {
                    "id": "fever",
                    "grade": "COMMON",
                    "probability": 0.5,
                    "note": "Sometimes present",
                },
            ],
            "differential_diagnosis": [],
            "risk_factors": [],
            "drugs": [],
            "vector_text_en": "Test",
            "stats": {
                "mortality_global_annual": None,
                "dalys_global": None,
                "incidence_rate_per_100k": None,
                "active_clinical_trials": None,
                "child_code_count": 0,
                "research_link_count": 0,
            },
            "ai_enriched": False,
            "last_updated": "2024-01-15",
        }
        with open(mms_dir / "AAA.00.yaml", "w") as f:
            yaml.dump(disease_data, f)
        
        index = build_reverse_index(sample_repo)
        fever_diseases = index["symptoms"]["fever"]["diseases"]
        codes = [d["code"] for d in fever_diseases]
        
        assert codes == sorted(codes)


class TestValidationFailures:
    """Test that validation fails appropriately."""
    
    def test_unknown_symptom_id_fails(self, sample_repo: Path):
        """Referencing unknown symptom should fail."""
        mms_dir = sample_repo / "data" / "mms"
        disease_data = {
            "entity_uri": "https://example.com/disease",
            "code": "BAD.01",
            "title_en": "Bad Disease",
            "definition_en": "A bad disease.",
            "parent_code": "BAD",
            "children_codes": [],
            "pathophysiology_en": "Test.",
            "symptoms": [
                {
                    "id": "nonexistent_symptom",
                    "grade": "COMMON",
                    "probability": 0.5,
                    "note": "",
                },
            ],
            "differential_diagnosis": [],
            "risk_factors": [],
            "drugs": [],
            "vector_text_en": "Test",
            "stats": {
                "mortality_global_annual": None,
                "dalys_global": None,
                "incidence_rate_per_100k": None,
                "active_clinical_trials": None,
                "child_code_count": 0,
                "research_link_count": 0,
            },
            "ai_enriched": False,
            "last_updated": "2024-01-15",
        }
        with open(mms_dir / "BAD.01.yaml", "w") as f:
            yaml.dump(disease_data, f)
        
        with pytest.raises(ValueError, match="nonexistent_symptom"):
            build_reverse_index(sample_repo)
    
    def test_symptom_filename_id_mismatch_fails(self, sample_repo: Path):
        """Symptom filename/id mismatch should fail."""
        foundation_dir = sample_repo / "data" / "foundation"
        # Create symptom with mismatched id
        mismatch_data = {
            "id": "wrong_id",
            "title_en": "Wrong ID",
            "definition_en": "This has wrong id.",
            "related_systems": [],
        }
        with open(foundation_dir / "correct_name.yaml", "w") as f:
            yaml.dump(mismatch_data, f)
        
        with pytest.raises(ValueError, match="does not match filename"):
            build_reverse_index(sample_repo)
    
    def test_missing_disease_code_fails(self, sample_repo: Path):
        """Missing disease code should fail."""
        mms_dir = sample_repo / "data" / "mms"
        disease_data = {
            "entity_uri": "https://example.com/disease",
            "code": "",  # Empty code
            "title_en": "No Code Disease",
            "definition_en": "A disease with no code.",
            "parent_code": None,
            "children_codes": [],
            "pathophysiology_en": "Test.",
            "symptoms": [],
            "differential_diagnosis": [],
            "risk_factors": [],
            "drugs": [],
            "vector_text_en": "Test",
            "stats": {
                "mortality_global_annual": None,
                "dalys_global": None,
                "incidence_rate_per_100k": None,
                "active_clinical_trials": None,
                "child_code_count": 0,
                "research_link_count": 0,
            },
            "ai_enriched": False,
            "last_updated": "2024-01-15",
        }
        with open(mms_dir / "NOCODE.yaml", "w") as f:
            yaml.dump(disease_data, f)
        
        with pytest.raises(ValueError, match="Missing or empty 'code'"):
            build_reverse_index(sample_repo)
    
    def test_missing_disease_title_fails(self, sample_repo: Path):
        """Missing disease title should fail."""
        mms_dir = sample_repo / "data" / "mms"
        disease_data = {
            "entity_uri": "https://example.com/disease",
            "code": "NOTITLE.01",
            "title_en": "",  # Empty title
            "definition_en": "A disease with no title.",
            "parent_code": None,
            "children_codes": [],
            "pathophysiology_en": "Test.",
            "symptoms": [],
            "differential_diagnosis": [],
            "risk_factors": [],
            "drugs": [],
            "vector_text_en": "Test",
            "stats": {
                "mortality_global_annual": None,
                "dalys_global": None,
                "incidence_rate_per_100k": None,
                "active_clinical_trials": None,
                "child_code_count": 0,
                "research_link_count": 0,
            },
            "ai_enriched": False,
            "last_updated": "2024-01-15",
        }
        with open(mms_dir / "NOTITLE.01.yaml", "w") as f:
            yaml.dump(disease_data, f)
        
        with pytest.raises(ValueError, match="Missing or empty 'title_en'"):
            build_reverse_index(sample_repo)
    
    def test_missing_symptom_id_in_reference_fails(self, sample_repo: Path):
        """Symptom reference missing id should fail."""
        mms_dir = sample_repo / "data" / "mms"
        disease_data = {
            "entity_uri": "https://example.com/disease",
            "code": "NOSYMID.01",
            "title_en": "No Sym ID Disease",
            "definition_en": "A disease.",
            "parent_code": None,
            "children_codes": [],
            "pathophysiology_en": "Test.",
            "symptoms": [
                {
                    # Missing "id" field
                    "grade": "COMMON",
                    "probability": 0.5,
                    "note": "",
                },
            ],
            "differential_diagnosis": [],
            "risk_factors": [],
            "drugs": [],
            "vector_text_en": "Test",
            "stats": {
                "mortality_global_annual": None,
                "dalys_global": None,
                "incidence_rate_per_100k": None,
                "active_clinical_trials": None,
                "child_code_count": 0,
                "research_link_count": 0,
            },
            "ai_enriched": False,
            "last_updated": "2024-01-15",
        }
        with open(mms_dir / "NOSYMID.01.yaml", "w") as f:
            yaml.dump(disease_data, f)
        
        with pytest.raises(ValueError, match="missing 'id'"):
            build_reverse_index(sample_repo)


class TestCheckMode:
    """Test --check mode functionality."""
    
    def test_check_passes_after_generation(self, sample_repo: Path):
        """Check should pass after generating links.yaml."""
        index = build_reverse_index(sample_repo)
        output_file = sample_repo / "data" / "generated" / "links.yaml"
        write_reverse_index(output_file, index)
        
        errors = check_reverse_index(sample_repo)
        assert len(errors) == 0
    
    def test_check_fails_when_links_missing(self, sample_repo: Path):
        """Check should fail when links.yaml is missing."""
        errors = check_reverse_index(sample_repo)
        
        assert len(errors) == 1
        assert "missing" in errors[0].lower()
    
    def test_check_fails_when_links_stale(self, sample_repo: Path):
        """Check should fail when links.yaml is stale."""
        # Generate initial index
        index = build_reverse_index(sample_repo)
        output_file = sample_repo / "data" / "generated" / "links.yaml"
        write_reverse_index(output_file, index)
        
        # Modify the disease to make index stale
        mms_dir = sample_repo / "data" / "mms"
        disease_data = {
            "entity_uri": "https://example.com/disease",
            "code": "NEW.01",
            "title_en": "New Disease",
            "definition_en": "A new disease.",
            "parent_code": "NEW",
            "children_codes": [],
            "pathophysiology_en": "Test.",
            "symptoms": [
                {
                    "id": "fever",
                    "grade": "RARE",
                    "probability": 0.01,
                    "note": "Rarely present",
                },
            ],
            "differential_diagnosis": [],
            "risk_factors": [],
            "drugs": [],
            "vector_text_en": "Test",
            "stats": {
                "mortality_global_annual": None,
                "dalys_global": None,
                "incidence_rate_per_100k": None,
                "active_clinical_trials": None,
                "child_code_count": 0,
                "research_link_count": 0,
            },
            "ai_enriched": False,
            "last_updated": "2024-01-15",
        }
        with open(mms_dir / "NEW.01.yaml", "w") as f:
            yaml.dump(disease_data, f)
        
        errors = check_reverse_index(sample_repo)
        
        assert len(errors) == 1
        assert "stale" in errors[0].lower()


class TestValidateIntegration:
    """Test integration with validate.py."""
    
    def test_validate_fails_when_reverse_index_stale(self, sample_repo: Path):
        """validate.py should fail when reverse index is stale."""
        # First generate valid index
        index = build_reverse_index(sample_repo)
        output_file = sample_repo / "data" / "generated" / "links.yaml"
        write_reverse_index(output_file, index)
        
        # Verify it passes initially
        errors = check_reverse_index(sample_repo)
        assert len(errors) == 0
        
        # Now add a new disease to make it stale
        mms_dir = sample_repo / "data" / "mms"
        disease_data = {
            "entity_uri": "https://example.com/disease",
            "code": "STALE.01",
            "title_en": "Stale Disease",
            "definition_en": "Makes index stale.",
            "parent_code": "STALE",
            "children_codes": [],
            "pathophysiology_en": "Test.",
            "symptoms": [
                {
                    "id": "fever",
                    "grade": "COMMON",
                    "probability": 0.5,
                    "note": "",
                },
            ],
            "differential_diagnosis": [],
            "risk_factors": [],
            "drugs": [],
            "vector_text_en": "Test",
            "stats": {
                "mortality_global_annual": None,
                "dalys_global": None,
                "incidence_rate_per_100k": None,
                "active_clinical_trials": None,
                "child_code_count": 0,
                "research_link_count": 0,
            },
            "ai_enriched": False,
            "last_updated": "2024-01-15",
        }
        with open(mms_dir / "STALE.01.yaml", "w") as f:
            yaml.dump(disease_data, f)
        
        # Now check should fail
        errors = check_reverse_index(sample_repo)
        assert len(errors) == 1
        assert "stale" in errors[0].lower()
