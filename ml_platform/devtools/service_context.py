"""Load per-service deploy env and secrets for scripts and tests."""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from ml_platform.devtools.models import E2EConfig, ServiceContext
from ml_platform.devtools.paths import repo_root
from ml_platform.devtools.paths import repo_root


def load_deploy_env(service_id: str) -> dict[str, str]:
    path = repo_root() / "services" / service_id / "generated" / "dev" / "deploy.env"
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" not in line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        out[key] = value.strip().strip("'").strip('"')
    return out


def load_oidc_secret(service_id: str) -> str:
    if os.getenv("AUTH_CLIENT_SECRET"):
        return os.environ["AUTH_CLIENT_SECRET"]
    path = repo_root() / "services" / service_id / "secrets.local.yaml"
    if not path.is_file():
        return ""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    string_data = data.get("stringData") or data
    secret = string_data.get("oidc-client-secret") or string_data.get(
        "oidc_client_secret", ""
    )
    return str(secret).strip()


def load_service_context(service_id: str) -> ServiceContext:
    env = load_deploy_env(service_id)
    api_port = env.get("PF_PORT", "8080")
    auth_port = env.get("AUTHENTIK_PF_PORT", "9001")
    fqdn = env.get(
        "AUTHENTIK_HOST_FQDN",
        "authentik-service.authentik-reusable-ml-services.svc.cluster.local",
    )
    return ServiceContext(
        service_id=service_id,
        namespace=env.get("NAMESPACE", service_id),
        base_url=os.getenv("BASE_URL", f"http://localhost:{api_port}"),
        auth_token_url=os.getenv(
            "AUTH_TOKEN_URL", f"http://localhost:{auth_port}/application/o/token/"
        ),
        auth_client_id=env.get("OIDC_CLIENT_ID", f"{service_id}-api"),
        auth_host_header=os.getenv("AUTH_HOST_HEADER", f"{fqdn}:9000"),
        auth_client_secret=load_oidc_secret(service_id),
    )


def apply_service_defaults(service_id: str) -> dict[str, str]:
    """Backward-compatible dict view for callers not yet migrated to ServiceContext."""
    ctx = load_service_context(service_id)
    return {
        "service_id": ctx.service_id,
        "namespace": ctx.namespace,
        "base_url": ctx.base_url,
        "auth_token_url": ctx.auth_token_url,
        "auth_client_id": ctx.auth_client_id,
        "auth_host_header": ctx.auth_host_header,
        "auth_client_secret": ctx.auth_client_secret,
    }
