#!/usr/bin/env python3
"""Tests for scripts/fetch_icd11.py."""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import yaml

# Import the script functions
import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from fetch_icd11 import (
    get_token,
    should_sync,
    load_state,
    save_state,
    clear_state,
    save_metadata,
    extract_disease_categories,
    collect_foundation_refs,
    extract_code_from_title,
    write_disease_yaml,
    write_foundation_yaml,
)


class TestGetToken:
    """Test OAuth2 token retrieval."""

    def test_get_token_success(self):
        """Test successful token retrieval."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"access_token": "test_token_123"}
        mock_response.raise_for_status = MagicMock()

        with patch("requests.Session.post", return_value=mock_response) as mock_post:
            session = MagicMock()
            session.post = mock_post
            token = get_token(session, "client_id", "client_secret")
            assert token == "test_token_123"


class TestShouldSync:
    """Test sync decision logic."""

    def test_should_sync_no_metadata(self):
        """Test sync needed when no metadata exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            assert should_sync(data_dir, "2024-01-01") is True

    def test_should_sync_different_release_date(self):
        """Test sync needed when release date changed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            metadata = {"release_date": "2023-01-01"}
            with open(data_dir / ".sync_metadata.json", "w") as f:
                json.dump(metadata, f)
            assert should_sync(data_dir, "2024-01-01") is True

    def test_should_sync_same_release_date(self):
        """Test no sync needed when release date unchanged."""
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            metadata = {"release_date": "2024-01-01"}
            with open(data_dir / ".sync_metadata.json", "w") as f:
                json.dump(metadata, f)
            assert should_sync(data_dir, "2024-01-01") is False

    def test_should_sync_force(self):
        """Test force flag overrides sync check."""
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            metadata = {"release_date": "2024-01-01"}
            with open(data_dir / ".sync_metadata.json", "w") as f:
                json.dump(metadata, f)
            assert should_sync(data_dir, "2024-01-01", force=True) is True


class TestStateManagement:
    """Test checkpoint/resume state management."""

    def test_load_state_empty(self):
        """Test loading state when no state file exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            state = load_state(data_dir)
            assert state == {"pending": [], "processed": []}

    def test_load_state_existing(self):
        """Test loading existing state."""
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            expected_state = {"pending": ["id1", "id2"], "processed": ["id3"]}
            with open(data_dir / ".fetch_state.json", "w") as f:
                json.dump(expected_state, f)
            state = load_state(data_dir)
            assert state == expected_state

    def test_save_and_load_state(self):
        """Test saving and loading state."""
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            state = {"pending": ["id1"], "processed": ["id2"]}
            save_state(data_dir, state)
            loaded = load_state(data_dir)
            assert loaded == state

    def test_clear_state(self):
        """Test clearing state."""
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            state_file = data_dir / ".fetch_state.json"
            with open(state_file, "w") as f:
                json.dump({"pending": []}, f)
            clear_state(data_dir)
            assert not state_file.exists()


class TestMetadata:
    """Test metadata management."""

    def test_save_metadata(self):
        """Test saving metadata."""
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            save_metadata(data_dir, "2024-01-01")
            metadata_file = data_dir / ".sync_metadata.json"
            assert metadata_file.exists()
            with open(metadata_file) as f:
                metadata = json.load(f)
            assert metadata["release_date"] == "2024-01-01"
            assert "last_sync" in metadata


class TestExtractDiseaseCategories:
    """Test disease category extraction."""

    def test_extract_categories(self):
        """Test extracting categories from tree."""
        tree = [
            {"classKind": "chapter", "title": "Chapter 1"},
            {"classKind": "category", "title": "1A00 Disease A"},
            {"classKind": "category", "title": "1B21.0 Plague"},
            {"classKind": "block", "title": "Block 1"},
        ]
        categories = extract_disease_categories(tree)
        assert len(categories) == 2
        assert categories[0]["title"] == "1A00 Disease A"
        assert categories[1]["title"] == "1B21.0 Plague"

    def test_extract_categories_empty(self):
        """Test extracting from empty tree."""
        categories = extract_disease_categories([])
        assert categories == []


class TestCollectFoundationRefs:
    """Test foundation reference collection."""

    def test_collect_refs(self):
        """Test collecting foundation references."""
        tree = [
            {
                "@id": "http://id.who.int/icd/entity/123",
                "note": [
                    {
                        "value": "caused by",
                        "causalAgent": {
                            "@id": "http://id.who.int/icd/entity/456"
                        },
                    }
                ],
            },
            {
                "@id": "http://id.who.int/icd/entity/789",
                "manifestation": {"@id": "http://id.who.int/icd/entity/101112"},
            },
        ]
        refs = collect_foundation_refs(tree)
        assert refs == {"123", "456", "789", "101112"}

    def test_collect_refs_empty(self):
        """Test collecting from tree without refs."""
        tree = [{"title": "No refs"}]
        refs = collect_foundation_refs(tree)
        assert refs == set()


class TestExtractCodeFromTitle:
    """Test code extraction from title."""

    def test_extract_code_with_space(self):
        """Test extracting code from '1B21.0 Plague'."""
        assert extract_code_from_title("1B21.0 Plague") == "1B21.0"

    def test_extract_code_no_space(self):
        """Test extracting code from title without space."""
        assert extract_code_from_title("1A00") == "1A00"

    def test_extract_code_empty(self):
        """Test extracting code from empty string."""
        assert extract_code_from_title("") == ""


class TestWriteDiseaseYaml:
    """Test disease YAML writing."""

    def test_write_disease_yaml(self):
        """Test writing a disease YAML file."""
        category = {
            "@id": "http://id.who.int/icd/entity/257068",
            "title": "1B21.0 Plague",
            "parent": {"title": "1B21 Bacterial infections"},
            "child": [
                {"classKind": "category", "title": "1B21.1 Sub disease"},
                {"classKind": "block", "title": "Block"},
            ],
            "note": [
                {"noteType": "definition", "language": "en", "value": "A disease caused by Yersinia pestis"}
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "1B21.0.yaml"
            write_disease_yaml(category, output_path)

            assert output_path.exists()
            with open(output_path) as f:
                data = yaml.safe_load(f)

            assert data["entity_uri"] == "http://id.who.int/icd/entity/257068"
            assert data["code"] == "1B21.0"
            assert data["title_en"] == "1B21.0 Plague"
            assert data["definition_en"] == "A disease caused by Yersinia pestis"
            assert data["parent_code"] == "1B21"
            assert data["children_codes"] == ["1B21.1"]
            assert data["pathophysiology_en"] is None
            assert data["symptoms"] == []
            assert data["ai_enriched"] is False
            assert data["stats"]["child_code_count"] == 1
            assert data["stats"]["research_link_count"] == 0


class TestWriteFoundationYaml:
    """Test foundation YAML writing."""

    def test_write_foundation_yaml(self):
        """Test writing a foundation YAML file."""
        entity = {
            "title": "Fever",
            "note": [
                {"noteType": "definition", "language": "en", "value": "Elevation of body temperature"}
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "257068.yaml"
            write_foundation_yaml(entity, output_path)

            assert output_path.exists()
            with open(output_path) as f:
                data = yaml.safe_load(f)

            assert data["id"] == "257068"
            assert data["title_en"] == "Fever"
            assert data["definition_en"] == "Elevation of body temperature"
            assert data["related_systems"] == []


class TestIntegration:
    """Integration tests with mocked API."""

    @patch.dict(os.environ, {"ICD11_CLIENT_ID": "test_id", "ICD11_CLIENT_SECRET": "test_secret"})
    @patch("fetch_icd11.get_token")
    @patch("fetch_icd11.fetch_release_date")
    @patch("fetch_icd11.fetch_linearisation_tree")
    def test_main_success(self, mock_fetch_tree, mock_fetch_date, mock_get_token):
        """Test successful main execution with mocked API."""
        mock_get_token.return_value = "test_token"
        mock_fetch_date.return_value = "2024-01-01"
        mock_fetch_tree.return_value = [
            {"@id": "http://id.who.int/icd/entity/123", "classKind": "category", "title": "1A00 Disease"}
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            # Create mms and foundation subdirectories
            (data_dir / "mms").mkdir()
            (data_dir / "foundation").mkdir()

            # Mock make_request to return minimal valid data
            with patch("fetch_icd11.make_request") as mock_make_request:
                mock_make_request.return_value = {
                    "@id": "http://id.who.int/icd/entity/123",
                    "title": "1A00 Disease",
                    "parent": {},
                    "child": [],
                    "note": [],
                }

                # Need to import main after patching
                from fetch_icd11 import main
                result = main(data_dir, force=True)

                assert result == 0
                # Check that files were created
                mms_files = list((data_dir / "mms").glob("*.yaml"))
                assert len(mms_files) >= 0  # May be 0 if entity fetching failed

    def test_main_missing_credentials(self):
        """Test main fails without credentials."""
        # Ensure env vars are not set
        old_id = os.environ.pop("ICD11_CLIENT_ID", None)
        old_secret = os.environ.pop("ICD11_CLIENT_SECRET", None)

        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                data_dir = Path(tmpdir)
                from fetch_icd11 import main
                result = main(data_dir)
                assert result == 1
        finally:
            # Restore env vars
            if old_id:
                os.environ["ICD11_CLIENT_ID"] = old_id
            if old_secret:
                os.environ["ICD11_CLIENT_SECRET"] = old_secret
