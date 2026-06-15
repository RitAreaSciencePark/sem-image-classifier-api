"""Authentication rejection tests."""

from __future__ import annotations

import requests


def test_unauthenticated_returns_401(base_url: str) -> None:
    """Protected endpoints reject requests without JWT."""
    endpoints = [
        ("POST", "/api/v1/inference", {"image_url": "http://example.com/test.jpg"}),
        ("POST", "/api/v1/jobs/status", {"job_id": "fake-id"}),
        ("POST", "/api/v1/jobs/results", {"job_id": "fake-id"}),
    ]
    for _method, path, body in endpoints:
        response = requests.post(f"{base_url}{path}", json=body, timeout=10)
        assert response.status_code == 401, (
            f"{path}: expected 401, got {response.status_code}"
        )


def test_invalid_token_returns_401(base_url: str) -> None:
    """Protected endpoints reject requests with garbage JWT."""
    headers = {"Authorization": "Bearer garbage.invalid.token"}
    response = requests.post(
        f"{base_url}/api/v1/inference",
        json={"image_url": "http://example.com/test.jpg"},
        headers=headers,
        timeout=10,
    )
    assert response.status_code in (401, 403), (
        f"Expected 401/403, got {response.status_code}"
    )
