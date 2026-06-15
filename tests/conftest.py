"""Pytest fixtures for multi-service E2E tests."""

from __future__ import annotations

import os

import pytest
import requests

from ml_platform.devtools.api_client import InferenceClient, get_token
from ml_platform.devtools.e2e_config import resolve_e2e_config
from ml_platform.devtools.models import E2EConfig, ServiceContext
from ml_platform.devtools.service_context import load_service_context


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "e2e: requires live cluster and port-forward"
    )


@pytest.fixture(scope="session")
def service_id() -> str:
    return os.getenv("SERVICE", "sem-classifier")


@pytest.fixture(scope="session")
def ctx(service_id: str) -> ServiceContext:
    return load_service_context(service_id)


@pytest.fixture(scope="session")
def e2e_config(service_id: str) -> E2EConfig:
    return resolve_e2e_config(service_id)


@pytest.fixture(scope="session")
def base_url(ctx: ServiceContext) -> str:
    _preflight_gateway(ctx.base_url)
    return ctx.base_url


@pytest.fixture(scope="session")
def token(ctx: ServiceContext) -> str:
    if not ctx.auth_client_secret:
        pytest.fail(
            "AUTH_CLIENT_SECRET is required (set env or services/<id>/secrets.local.yaml)"
        )
    return get_token(
        ctx.auth_token_url,
        ctx.auth_client_id,
        ctx.auth_client_secret,
        ctx.auth_host_header,
    )


@pytest.fixture(scope="session")
def inference_client(base_url: str, token: str, e2e_config: E2EConfig) -> InferenceClient:
    return InferenceClient(
        base_url,
        token,
        default_timeout=e2e_config.inference_timeout_s,
    )


def _preflight_gateway(base_url: str) -> None:
    try:
        response = requests.get(f"{base_url.rstrip('/')}/__health", timeout=5)
        response.raise_for_status()
    except requests.RequestException as exc:
        pytest.exit(
            f"E2E preflight failed: gateway not reachable at {base_url} ({exc}). "
            "Run: make access SERVICE=<id>",
            returncode=1,
        )
