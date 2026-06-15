"""Unit tests for optional per-service E2E config."""

from __future__ import annotations

import ml_platform.devtools.e2e_config as e2e_config
from ml_platform.devtools.api_client import DEFAULT_IMAGE_URL
from ml_platform.devtools.e2e_config import load_e2e_config, resolve_e2e_config


def test_resolve_e2e_config_uses_default_image_when_blank(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(e2e_config, "repo_root", lambda: tmp_path)

    resolved = resolve_e2e_config("sem-classifier")

    assert resolved.test_image_url == DEFAULT_IMAGE_URL
    assert resolved.inference_timeout_s == 60.0


def test_load_e2e_config_reads_service_file(tmp_path, monkeypatch) -> None:
    service_id = "sem-classifier"
    service_dir = tmp_path / "services" / service_id
    service_dir.mkdir(parents=True)
    (service_dir / "e2e.yaml").write_text(
        "test_image_url: https://example.com/test.jpg\n"
        "inference_timeout_s: 90\n"
        "expected_labels: [chip, wafer]\n"
    )
    monkeypatch.setattr(e2e_config, "repo_root", lambda: tmp_path)

    loaded = load_e2e_config(service_id)

    assert loaded.test_image_url == "https://example.com/test.jpg"
    assert loaded.inference_timeout_s == 90.0
    assert loaded.expected_labels == ("chip", "wafer")
