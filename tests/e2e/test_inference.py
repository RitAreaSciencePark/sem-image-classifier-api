"""Authenticated inference flow tests."""

from __future__ import annotations

from ml_platform.devtools.api_client import InferenceClient
from ml_platform.devtools.models import E2EConfig


def test_inference_url(
    inference_client: InferenceClient, e2e_config: E2EConfig
) -> None:
    """Full inference flow: submit → poll → results (authenticated)."""
    result = inference_client.run_inference(
        e2e_config.test_image_url,
        timeout=e2e_config.inference_timeout_s,
    )
    assert "label" in result
    assert "confidence" in result
    if e2e_config.expected_labels:
        assert result["label"] in e2e_config.expected_labels
