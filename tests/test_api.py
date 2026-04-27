"""
End-to-End API Tests for SEM Classifier

Tests the full request flow through KrakenD → BentoML → Redis,
including JWT authentication via mock-oidc.

PREREQUISITES:
  1. Full stack running on K3s:  ./dev.sh deploy
  2. Port-forwards active:       ./dev.sh access
     - KrakenD on localhost:8080
     - mock-oidc on localhost:18080

Run:
    python tests/test_api.py
"""

import requests
import time
import sys
import os

try:
    import pytest
except ImportError:

    class _PytestCompat:
        @staticmethod
        def fixture(func=None, *args, **kwargs):
            if func is not None:
                return func

            def decorator(inner):
                return inner

            return decorator

    pytest = _PytestCompat()

BASE_URL = "http://localhost:8080"
TOKEN_PROVIDER = os.getenv("TOKEN_PROVIDER", "mock")
MOCK_TOKEN_URL = os.getenv("MOCK_TOKEN_URL", "http://localhost:18080/default/token")
AUTH_TOKEN_URL = os.getenv("AUTH_TOKEN_URL", "")
AUTH_CLIENT_ID = os.getenv("AUTH_CLIENT_ID", "")
AUTH_CLIENT_SECRET = os.getenv("AUTH_CLIENT_SECRET", "")
AUTH_SCOPE = os.getenv("AUTH_SCOPE", "openid profile email")


# ============================================================================
# TOKEN HELPER
# ============================================================================


def get_token():
    """Get a JWT from the configured token provider via client_credentials grant."""
    if TOKEN_PROVIDER == "authentik":
        assert AUTH_TOKEN_URL, (
            "AUTH_TOKEN_URL is required when TOKEN_PROVIDER=authentik"
        )
        assert AUTH_CLIENT_ID, (
            "AUTH_CLIENT_ID is required when TOKEN_PROVIDER=authentik"
        )
        assert AUTH_CLIENT_SECRET, (
            "AUTH_CLIENT_SECRET is required when TOKEN_PROVIDER=authentik"
        )
        resp = requests.post(
            AUTH_TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "scope": AUTH_SCOPE,
                "client_id": AUTH_CLIENT_ID,
                "client_secret": AUTH_CLIENT_SECRET,
            },
            timeout=10,
        )
    else:
        resp = requests.post(
            MOCK_TOKEN_URL,
            headers={"Host": "mock-oidc:8080"},
            data={
                "grant_type": "client_credentials",
                "scope": "openid",
                "client_id": "test-client",
                "client_secret": "test-secret",
            },
            timeout=5,
        )

    assert resp.status_code == 200, (
        f"Token request failed: {resp.status_code} {resp.text}"
    )
    token = resp.json().get("access_token")
    assert token, "No access_token in response"
    return token


def auth_headers(token):
    """Return Authorization header dict."""
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def token():
    """Provide JWT token for pytest test functions."""
    return get_token()


# ============================================================================
# TESTS: No auth required
# ============================================================================


def test_gateway_health():
    """KrakenD built-in health check — gateway process is alive."""
    print("[TEST] KrakenD gateway health...")
    r = requests.get(f"{BASE_URL}/__health")
    assert r.status_code == 200, f"Expected 200, got {r.status_code}"
    print(f"  OK: {r.status_code}")


def test_backend_health():
    """Backend health via KrakenD — checks BentoML model + Redis."""
    print("[TEST] Backend health...")
    r = requests.get(f"{BASE_URL}/health")
    assert r.status_code == 200, f"Expected 200, got {r.status_code}"
    data = r.json()
    assert data["model_loaded"] is True, "Model not loaded"
    assert data["redis_connected"] is True, "Redis not connected"
    print(f"  OK: status={data['status']}, device={data['device']}")


def test_api_version():
    """Static API discovery endpoint — no backend call."""
    print("[TEST] API version...")
    r = requests.get(f"{BASE_URL}/api/v1/version")
    assert r.status_code == 200, f"Expected 200, got {r.status_code}"
    data = r.json()
    assert "endpoints" in data, "Missing endpoints in version response"
    print(f"  OK: {data.get('api_version', 'unknown')}")


# ============================================================================
# TESTS: Auth required
# ============================================================================


def test_unauthenticated_returns_401():
    """Protected endpoints reject requests without JWT."""
    print("[TEST] Unauthenticated → 401...")
    endpoints = [
        ("POST", "/api/v1/inference", {"image_url": "http://example.com/test.jpg"}),
        ("POST", "/api/v1/jobs/status", {"job_id": "fake-id"}),
        ("POST", "/api/v1/jobs/results", {"job_id": "fake-id"}),
    ]
    for method, path, body in endpoints:
        r = requests.post(f"{BASE_URL}{path}", json=body)
        assert r.status_code == 401, f"{path}: expected 401, got {r.status_code}"
        print(f"  OK: {path} → 401")


def test_invalid_token_returns_401():
    """Protected endpoints reject requests with garbage JWT."""
    print("[TEST] Invalid token → 401...")
    headers = {"Authorization": "Bearer garbage.invalid.token"}
    r = requests.post(
        f"{BASE_URL}/api/v1/inference",
        json={"image_url": "http://example.com/test.jpg"},
        headers=headers,
    )
    assert r.status_code in (401, 403), f"Expected 401/403, got {r.status_code}"
    print(f"  OK: garbage token → {r.status_code}")


def test_inference_url(token):
    """Full inference flow: submit → poll → results (authenticated)."""
    print("[TEST] Inference flow (URL input, authenticated)...")
    headers = auth_headers(token)

    # Real SEM image from Nebraska Center for Materials and Nanoscience.
    test_url = (
        "https://enif.unl.edu/sites/unl.edu.research.nebraska-center-for-materials-"
        "and-nanoscience.electron-nanoscopy/files/styles/no_crop_720/public/media/"
        "image/NanoSEM_15KV.jpg?itok=McoSOeAv"
    )

    # Step 1: Submit
    print("  Submitting job...")
    r = requests.post(
        f"{BASE_URL}/api/v1/inference",
        json={"image_url": test_url},
        headers=headers,
    )
    assert r.status_code == 200, f"Submit failed: {r.status_code} {r.text}"
    submit_data = r.json()
    job_id = submit_data["job_id"]
    print(f"  Job ID: {job_id}")

    # Step 2: Poll status until complete (max 60s)
    print("  Polling status...")
    for i in range(120):
        r = requests.post(
            f"{BASE_URL}/api/v1/jobs/status",
            json={"job_id": job_id},
            headers=headers,
        )
        assert r.status_code == 200, f"Status check failed: {r.status_code}"
        status_data = r.json()
        status = status_data["status"]

        if status == "COMPLETED":
            print(f"  Status: {status} (after {i * 0.5:.1f}s)")
            break
        elif status == "FAILED":
            print(f"  FAILED after {i * 0.5:.1f}s")
            break

        time.sleep(0.5)
    else:
        raise AssertionError("Job did not complete within 60s")

    # Step 3: Get results
    print("  Fetching results...")
    r = requests.post(
        f"{BASE_URL}/api/v1/jobs/results",
        json={"job_id": job_id},
        headers=headers,
    )
    assert r.status_code == 200, f"Results failed: {r.status_code}"
    result_data = r.json()
    assert result_data["status"] == "COMPLETED", (
        f"Expected COMPLETED, got {result_data['status']}"
    )
    assert result_data["result"] is not None, "No result in response"

    result = result_data["result"]
    print(f"  Label: {result['label']}")
    print(f"  Confidence: {result['confidence']:.4f}")
    print(f"  Device: {result['device_used']}")
    print(f"  All scores: { {k: f'{v:.4f}' for k, v in result['all_scores'].items()} }")


# ============================================================================
# MAIN
# ============================================================================


def main():
    print(f"\n{'=' * 60}")
    print("SEM Classifier API — End-to-End Tests")
    print(f"Target: {BASE_URL}")
    print(f"{'=' * 60}\n")
    print(f"[SETUP] Token provider: {TOKEN_PROVIDER}")

    # Get a JWT for authenticated tests
    print("[SETUP] Acquiring JWT for authenticated tests...")
    try:
        token = get_token()
        print(f"  OK: token acquired ({len(token)} chars)\n")
    except Exception as e:
        print(f"  FAILED: {e}")
        if TOKEN_PROVIDER == "authentik":
            print("  Check AUTH_TOKEN_URL, AUTH_CLIENT_ID, AUTH_CLIENT_SECRET")
        else:
            print("  Is mock-oidc port-forwarded? Run: ./dev.sh access")
        sys.exit(1)

    tests = [
        # No-auth tests
        ("test_gateway_health", lambda: test_gateway_health()),
        ("test_backend_health", lambda: test_backend_health()),
        ("test_api_version", lambda: test_api_version()),
        # Auth rejection tests
        (
            "test_unauthenticated_returns_401",
            lambda: test_unauthenticated_returns_401(),
        ),
        ("test_invalid_token_returns_401", lambda: test_invalid_token_returns_401()),
        # Authenticated flow test
        ("test_inference_url", lambda: test_inference_url(token)),
    ]

    passed = 0
    failed = 0

    for name, test_fn in tests:
        try:
            result = test_fn()
            if result is not False:
                passed += 1
            else:
                failed += 1
        except Exception as e:
            print(f"  FAILED: {e}")
            failed += 1

    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    print(f"{'=' * 60}\n")

    sys.exit(1 if failed > 0 else 0)


if __name__ == "__main__":
    main()
