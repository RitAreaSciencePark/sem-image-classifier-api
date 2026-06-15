"""Optional per-service E2E overrides from services/<id>/e2e.yaml."""

from __future__ import annotations

from pathlib import Path

import yaml

from ml_platform.devtools.api_client import DEFAULT_IMAGE_URL
from ml_platform.devtools.models import E2EConfig
from ml_platform.devtools.paths import repo_root


def load_e2e_config(service_id: str) -> E2EConfig:
    path = repo_root() / "services" / service_id / "e2e.yaml"
    if not path.is_file():
        return E2EConfig()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    labels = data.get("expected_labels") or []
    return E2EConfig(
        test_image_url=str(data.get("test_image_url", "")).strip(),
        inference_timeout_s=float(data.get("inference_timeout_s", 60.0)),
        expected_labels=tuple(str(label) for label in labels),
    )


def resolve_e2e_config(service_id: str) -> E2EConfig:
    loaded = load_e2e_config(service_id)
    if loaded.test_image_url:
        return loaded
    return E2EConfig(
        test_image_url=DEFAULT_IMAGE_URL,
        inference_timeout_s=loaded.inference_timeout_s,
        expected_labels=loaded.expected_labels,
    )
