"""Health and discovery endpoint tests (no JWT required)."""

from __future__ import annotations

import requests


def test_gateway_health(base_url: str) -> None:
    """KrakenD built-in health check — gateway process is alive."""
    response = requests.get(f"{base_url}/__health", timeout=10)
    assert response.status_code == 200, f"Expected 200, got {response.status_code}"


def test_backend_health(base_url: str) -> None:
    """Backend health via KrakenD — checks BentoML model + Redis."""
    response = requests.get(f"{base_url}/health", timeout=10)
    assert response.status_code == 200, f"Expected 200, got {response.status_code}"
    data = response.json()
    assert data["model_loaded"] is True, "Model not loaded"
    assert data["redis_connected"] is True, "Redis not connected"


def test_api_version(base_url: str) -> None:
    """Static API discovery endpoint — no backend call."""
    response = requests.get(f"{base_url}/api/v1/version", timeout=10)
    assert response.status_code == 200, f"Expected 200, got {response.status_code}"
    data = response.json()
    assert "endpoints" in data, "Missing endpoints in version response"
