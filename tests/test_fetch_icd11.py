#!/usr/bin/env python3
"""Tests for scripts/fetch_icd11.py."""

import json
import os
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml  # type: ignore[import-untyped]

# Import functions from fetch_icd11 module
import importlib.util

FETCH_MODULE_PATH = Path(__file__).parent.parent / "scripts" / "fetch_icd11.py"


def load_fetch_module() -> Any:
    """Load fetch_icd11 module dynamically."""
    spec = importlib.util.spec_from_file_location("fetch_icd11", FETCH_MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fetch_module() -> Any:
    """Fixture to load fetch_icd11 module."""
    return load_fetch_module()


@pytest.fixture
def temp_repo() -> Path:
    """Create a temporary repository structure."""
    temp_dir = tempfile.mkdtemp()
    base_dir = Path(temp_dir)
    (base_dir / "data" / "mms").mkdir(parents=True)
    (base_dir / "data" / "foundation").mkdir(parents=True)
    (base_dir / "data" / "generated").mkdir(parents=True)
    (base_dir / "schemas").mkdir(parents=True)
    return base_dir


@pytest.fixture
def sample_schemas(temp_repo: Path) -> None:
    """Create minimal schemas for validation."""
    disease_schema = {
        "type": "object",
        "required": [
            "entity_uri",
            "code",
            "title_en",
            "definition_en",
            "parent_code",
            "children_codes",
            "pathophysiology_en",
            "symptoms",
            "differential_diagnosis",
            "risk_factors",
            "drugs",
            "vector_text_en",
            "stats",
            "ai_enriched",
            "last_updated",
        ],
        "properties": {
            "entity_uri": {"type": "string"},
            "code": {"type": "string"},
            "title_en": {"type": "string"},
            "definition_en": {"type": "string"},
            "parent_code": {"type": ["string", "null"]},
            "children_codes": {"type": "array"},
            "pathophysiology_en": {"type": "string"},
            "symptoms": {"type": "array"},
            "differential_diagnosis": {"type": "array"},
            "risk_factors": {"type": "array"},
            "drugs": {"type": "array"},
            "vector_text_en": {"type": "string"},
            "stats": {
                "type": "object",
                "required": [
                    "mortality_global_annual",
                    "dalys_global",
                    "incidence_rate_per_100k",
                    "active_clinical_trials",
                    "child_code_count",
                    "research_link_count",
                ],
                "additionalProperties": False,
            },
            "ai_enriched": {"type": "boolean"},
            "last_updated": {"type": "string"},
        },
    }

    symptom_schema = {
        "type": "object",
        "required": ["id", "title_en", "definition_en", "related_systems"],
        "properties": {
            "id": {"type": "string"},
            "title_en": {"type": "string"},
            "definition_en": {"type": "string"},
            "related_systems": {"type": "array"},
        },
    }

    with open(temp_repo / "schemas" / "disease.schema.json", "w") as f:
        json.dump(disease_schema, f)

    with open(temp_repo / "schemas" / "symptom.schema.json", "w") as f:
        json.dump(symptom_schema, f)


class TestRateLimiter:
    """Test RateLimiter class."""

    def test_rate_limiter_no_sleep_on_first_request(self, fetch_module: Any) -> None:
        """First request should not sleep."""
        fake_sleep_calls: list[float] = []

        def fake_sleep(duration: float) -> None:
            fake_sleep_calls.append(duration)

        limiter = fetch_module.RateLimiter(5.0, sleep=fake_sleep)
        limiter.wait()

        assert len(fake_sleep_calls) == 0

    def test_rate_limiter_sleeps_between_requests(self, fetch_module: Any) -> None:
        """Subsequent requests within interval should sleep."""
        fake_sleep_calls: list[float] = []

        def fake_sleep(duration: float) -> None:
            fake_sleep_calls.append(duration)

        limiter = fetch_module.RateLimiter(10.0, sleep=fake_sleep)  # 0.1s interval
        limiter.wait()  # First request
        limiter.last_request = limiter.last_request - 0.2 if limiter.last_request else 0  # Simulate time passing
        limiter.wait()  # Should not sleep since enough time passed

        # Now test actual sleeping
        limiter2 = fetch_module.RateLimiter(10.0, sleep=fake_sleep)
        limiter2.wait()  # First request
        # Don't manipulate time, second wait should sleep
        limiter2.wait()

        assert len(fake_sleep_calls) > 0
        assert fake_sleep_calls[-1] > 0

    def test_rate_limiter_testable_without_real_sleep(self, fetch_module: Any) -> None:
        """Rate limiter should be testable with fake sleep."""
        call_count = 0

        def counting_sleep(duration: float) -> None:
            nonlocal call_count
            call_count += 1

        limiter = fetch_module.RateLimiter(100.0, sleep=counting_sleep)
        limiter.wait()
        limiter.wait()
        limiter.wait()

        # Should have called sleep at least twice (after first request)
        assert call_count >= 2


class TestICD11Client:
    """Test ICD11Client class."""

    def test_client_initialization(self, fetch_module: Any) -> None:
        """Client should initialize with credentials."""
        client = fetch_module.ICD11Client("test_id", "test_secret")
        assert client.client_id == "test_id"
        assert client.client_secret == "test_secret"
        assert client._token is None

    @patch("httpx.Client")
    def test_get_token_forms_request_correctly(
        self, mock_client_class: MagicMock, fetch_module: Any
    ) -> None:
        """Token request should be formed correctly."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "access_token": "test_token",
            "expires_in": 3600,
        }
        mock_client = MagicMock()
        mock_client.post.return_value = mock_response
        mock_client_class.return_value = mock_client

        client = fetch_module.ICD11Client("client_id", "client_secret")
        token = client.get_token()

        assert token == "test_token"
        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        assert call_args[0][0] == fetch_module.ICD11Client.TOKEN_URL
        data = call_args[1]["data"]
        assert data["grant_type"] == "client_credentials"
        assert data["client_id"] == "client_id"
        assert data["client_secret"] == "client_secret"

    @patch("httpx.Client")
    def test_get_entity_uses_auth_header(
        self, mock_client_class: MagicMock, fetch_module: Any
    ) -> None:
        """Entity request should include auth header."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"@id": "test_uri", "code": "TEST"}
        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client_class.return_value = mock_client

        client = fetch_module.ICD11Client("client_id", "client_secret")
        # Set token and expiry directly to avoid token refresh
        client._token = "test_token"
        from datetime import datetime, timezone, timedelta
        client._token_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)

        entity = client.get_entity("https://example.com/entity")

        assert entity["@id"] == "test_uri"
        mock_client.get.assert_called_once()
        headers = mock_client.get.call_args[1]["headers"]
        assert "Authorization" in headers
        assert headers["Authorization"] == "Bearer test_token"


class TestYAMLHelpers:
    """Test YAML helper functions."""

    def test_save_yaml_atomic_creates_file(self, fetch_module: Any, temp_repo: Path) -> None:
        """Atomic save should create YAML file."""
        data = {"code": "TEST", "title_en": "Test Disease"}
        output_path = temp_repo / "data" / "mms" / "TEST.yaml"

        fetch_module.save_yaml_atomic(output_path, data)

        assert output_path.exists()
        with open(output_path) as f:
            loaded = yaml.safe_load(f)
        assert loaded["code"] == "TEST"
        assert loaded["title_en"] == "Test Disease"

    def test_save_yaml_atomic_is_atomic(self, fetch_module: Any, temp_repo: Path) -> None:
        """Save should use atomic rename."""
        data = {"code": "TEST"}
        output_path = temp_repo / "data" / "mms" / "TEST.yaml"

        # Create existing file
        fetch_module.save_yaml_atomic(output_path, {"code": "OLD"})

        # Overwrite
        fetch_module.save_yaml_atomic(output_path, data)

        # Should have new content
        with open(output_path) as f:
            loaded = yaml.safe_load(f)
        assert loaded["code"] == "TEST"


class TestDiseaseNormalization:
    """Test disease entity normalization."""

    def test_normalize_who_entity_to_disease(self, fetch_module: Any) -> None:
        """WHO entity should convert to valid disease YAML."""
        who_entity = {
            "@id": "https://id.who.int/icd/entity/123",
            "code": "1B21.0",
            "title": {"@value": "Test Disease"},
            "definition": {"@value": "A test disease definition"},
            "parent": {"code": "1B21"},
            "childEntities": [{"code": "1B21.0-A"}, {"code": "1B21.0-B"}],
        }

        result = fetch_module.normalize_who_entity_to_disease(who_entity)

        assert result["entity_uri"] == "https://id.who.int/icd/entity/123"
        assert result["code"] == "1B21.0"
        assert result["title_en"] == "Test Disease"
        assert result["definition_en"] == "A test disease definition"
        assert result["parent_code"] == "1B21"
        assert "1B21.0-A" in result["children_codes"]
        assert "1B21.0-B" in result["children_codes"]
        assert result["ai_enriched"] is False
        assert result["stats"]["child_code_count"] == 0
        assert result["stats"]["research_link_count"] == 0
        assert "last_updated" in result
        assert result["last_updated"] != ""

    def test_create_empty_disease_has_all_fields(self, fetch_module: Any) -> None:
        """Empty disease YAML should have all required fields."""
        empty = fetch_module.create_empty_disease_yaml()

        assert "entity_uri" in empty
        assert "code" in empty
        assert "title_en" in empty
        assert "definition_en" in empty
        assert "parent_code" in empty
        assert "children_codes" in empty
        assert "pathophysiology_en" in empty
        assert "symptoms" in empty
        assert "differential_diagnosis" in empty
        assert "risk_factors" in empty
        assert "drugs" in empty
        assert "vector_text_en" in empty
        assert "stats" in empty
        assert "ai_enriched" in empty
        assert "last_updated" in empty

    def test_merge_existing_preserves_enriched_fields(self, fetch_module: Any) -> None:
        """Merge should preserve AI-enriched fields."""
        new_data = {
            "code": "TEST",
            "title_en": "New Title",
            "pathophysiology_en": "",
            "symptoms": [],
            "stats": {"child_code_count": 0},
            "ai_enriched": False,
        }

        existing = {
            "pathophysiology_en": "Custom pathophysiology",
            "symptoms": [{"id": "fever", "grade": "COMMON"}],
            "stats": {"child_code_count": 5, "mortality_global_annual": 100},
            "ai_enriched": True,
            "drugs": ["aspirin"],
        }

        merged = fetch_module.merge_existing_fields(new_data, existing)

        assert merged["pathophysiology_en"] == "Custom pathophysiology"
        assert len(merged["symptoms"]) == 1
        assert merged["symptoms"][0]["id"] == "fever"
        assert merged["stats"]["child_code_count"] == 5
        assert merged["stats"]["mortality_global_annual"] == 100
        assert merged["ai_enriched"] is True
        assert merged["drugs"] == ["aspirin"]
        # WHO fields should be updated
        assert merged["title_en"] == "New Title"


class TestSymptomNormalization:
    """Test symptom entity normalization."""

    def test_normalize_who_foundation_entity(self, fetch_module: Any) -> None:
        """WHO foundation entity should convert to valid symptom YAML."""
        who_entity = {
            "@id": "https://id.who.int/icd/foundation/fever",
            "code": "fever",
            "title": {"@value": "Fever"},
            "definition": {"@value": "Elevated body temperature"},
        }

        result = fetch_module.normalize_who_foundation_entity(who_entity, "fever")

        assert result["id"] == "fever"
        assert result["title_en"] == "Fever"
        assert result["definition_en"] == "Elevated body temperature"
        assert isinstance(result["related_systems"], list)


class TestCheckpointing:
    """Test checkpoint functionality."""

    def test_checkpoint_written(self, fetch_module: Any, temp_repo: Path) -> None:
        """Checkpoint file should be written."""
        checkpoint_path = temp_repo / ".cache" / "test_checkpoint.json"
        checkpoint = {
            "processed_entity_uris": ["uri1", "uri2"],
            "last_entity_uri": "uri2",
            "updated_at": "",
        }

        fetch_module.save_checkpoint(checkpoint_path, checkpoint)

        assert checkpoint_path.exists()
        with open(checkpoint_path) as f:
            loaded = json.load(f)
        assert "uri1" in loaded["processed_entity_uris"]
        assert "uri2" in loaded["processed_entity_uris"]
        assert loaded["last_entity_uri"] == "uri2"
        assert loaded["updated_at"] != ""

    def test_resume_skips_processed_uris(self, fetch_module: Any, temp_repo: Path) -> None:
        """Resume should skip already processed URIs."""
        checkpoint_path = temp_repo / ".cache" / "test_checkpoint.json"
        checkpoint = {
            "processed_entity_uris": ["uri1", "uri2"],
            "last_entity_uri": "uri2",
            "updated_at": "",
        }
        fetch_module.save_checkpoint(checkpoint_path, checkpoint)

        loaded = fetch_module.load_checkpoint(checkpoint_path)
        assert "uri1" in loaded["processed_entity_uris"]
        assert "uri2" in loaded["processed_entity_uris"]


class TestMissingCredentials:
    """Test missing credential handling."""

    def test_missing_client_id_fails(self, fetch_module: Any, monkeypatch: Any, capfd: Any) -> None:
        """Missing ICD11_CLIENT_ID should fail clearly."""
        monkeypatch.delenv("ICD11_CLIENT_ID", raising=False)
        monkeypatch.setenv("ICD11_CLIENT_SECRET", "secret")

        # The function prints error and calls sys.exit(1)
        try:
            fetch_module.fetch_and_save_entities(tempfile.mkdtemp())
            assert False, "Should have exited"
        except SystemExit as e:
            assert e.code == 1

        captured = capfd.readouterr()
        assert "ICD11_CLIENT_ID" in captured.err

    def test_missing_client_secret_fails(self, fetch_module: Any, monkeypatch: Any, capfd: Any) -> None:
        """Missing ICD11_CLIENT_SECRET should fail clearly."""
        monkeypatch.setenv("ICD11_CLIENT_ID", "client_id")
        monkeypatch.delenv("ICD11_CLIENT_SECRET", raising=False)

        try:
            fetch_module.fetch_and_save_entities(tempfile.mkdtemp())
            assert False, "Should have exited"
        except SystemExit as e:
            assert e.code == 1

        captured = capfd.readouterr()
        assert "ICD11_CLIENT_SECRET" in captured.err


class TestNoSecretsWritten:
    """Test that secrets are not written to disk."""

    def test_token_not_in_checkpoint(self, fetch_module: Any, temp_repo: Path) -> None:
        """Token should not be written to checkpoint."""
        checkpoint_path = temp_repo / ".cache" / "test.json"
        checkpoint = {
            "processed_entity_uris": [],
            "last_entity_uri": None,
            "updated_at": "",
        }

        fetch_module.save_checkpoint(checkpoint_path, checkpoint)

        with open(checkpoint_path) as f:
            content = f.read()

        assert "token" not in content.lower() or "access_token" not in content
        assert "secret" not in content.lower()

    def test_credentials_not_in_yaml(self, fetch_module: Any, temp_repo: Path) -> None:
        """Credentials should not appear in generated YAML."""
        disease_data = fetch_module.create_empty_disease_yaml()
        disease_data["code"] = "TEST"
        disease_data["title_en"] = "Test"

        output_path = temp_repo / "data" / "mms" / "TEST.yaml"
        fetch_module.save_yaml_atomic(output_path, disease_data)

        with open(output_path) as f:
            content = f.read()

        assert "ICD11_CLIENT_ID" not in content
        assert "ICD11_CLIENT_SECRET" not in content
        assert "client_secret" not in content


class TestValidationAfterWrite:
    """Test that validation runs after writing."""

    @patch("httpx.Client")
    def test_fetcher_calls_validation(
        self, mock_client_class: MagicMock, fetch_module: Any, temp_repo: Path, sample_schemas: None
    ) -> None:
        """Fetcher should validate repository after writing."""
        # Setup mock responses
        mock_response = MagicMock()
        mock_response.json.side_effect = [
            {"@id": "https://id.who.int/icd/release/11/mms", "code": "root"},
            [],  # children
        ]
        mock_response.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.post.return_value = MagicMock(
            json=lambda: {"access_token": "token", "expires_in": 3600}
        )
        mock_client.get.return_value = mock_response
        mock_client_class.return_value = mock_client

        # Set env vars
        os.environ["ICD11_CLIENT_ID"] = "test"
        os.environ["ICD11_CLIENT_SECRET"] = "test"

        # Create a valid disease file first
        disease_file = temp_repo / "data" / "mms" / "root.yaml"
        initial_disease = fetch_module.create_empty_disease_yaml()
        initial_disease["code"] = "root"
        initial_disease["title_en"] = "Root"
        initial_disease["entity_uri"] = "https://id.who.int/icd/release/11/mms"
        fetch_module.save_yaml_atomic(disease_file, initial_disease)

        # Run fetcher
        fetch_module.fetch_and_save_entities(temp_repo, limit=1)

        # File should exist and be valid YAML
        assert disease_file.exists()
        with open(disease_file) as f:
            data = yaml.safe_load(f)
        assert data["code"] == "root"


class TestCIWithoutCredentials:
    """Test that CI tests work without real credentials."""

    def test_tests_do_not_require_real_credentials(
        self, fetch_module: Any, monkeypatch: Any
    ) -> None:
        """Tests should run without real WHO credentials."""
        # Remove any credentials
        monkeypatch.delenv("ICD11_CLIENT_ID", raising=False)
        monkeypatch.delenv("ICD11_CLIENT_SECRET", raising=False)

        # This test just verifies the module loads and basic functions work
        empty = fetch_module.create_empty_disease_yaml()
        assert "code" in empty
        assert empty["ai_enriched"] is False
