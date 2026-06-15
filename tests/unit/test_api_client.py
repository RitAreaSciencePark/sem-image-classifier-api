"""Unit tests for API client helpers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from ml_platform.devtools.api_client import InferenceClient, auth_headers, poll_job


def test_auth_headers_prefixes_bearer_token() -> None:
    assert auth_headers("abc") == {"Authorization": "Bearer abc"}


def test_poll_job_returns_completed_status() -> None:
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"status": "COMPLETED"}

    with patch(
        "ml_platform.devtools.api_client.post_json", return_value=response
    ) as post_json:
        status = poll_job("http://localhost:8080", "token", "job-1", timeout=1.0)

    assert status == "COMPLETED"
    post_json.assert_called_once()


def test_inference_client_run_inference_happy_path() -> None:
    client = InferenceClient("http://localhost:8080", "token", default_timeout=5.0)

    submit = MagicMock(status_code=200, job_id="job-1", latency_s=0.1, error="")
    poll = MagicMock(
        status="COMPLETED",
        payload={"status": "COMPLETED"},
        error="",
    )
    results = {"status": "COMPLETED", "result": {"label": "foo", "confidence": 0.9}}

    with (
        patch.object(client, "submit", return_value=submit),
        patch.object(client, "poll_until_done", return_value=poll),
        patch.object(client, "get_results", return_value=results),
    ):
        result = client.run_inference("http://example.com/image.jpg")

    assert result["label"] == "foo"
