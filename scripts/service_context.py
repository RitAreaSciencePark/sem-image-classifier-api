"""Load per-service deploy env and secrets for scripts."""

from __future__ import annotations

import os
import re
from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


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
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r'^\s*oidc-client-secret:\s*"?([^"#]+)"?\s*$', line)
        if m:
            return m.group(1).strip()
    return ""


def apply_service_defaults(service_id: str) -> dict[str, str]:
    env = load_deploy_env(service_id)
    api_port = env.get("PF_PORT", "8080")
    auth_port = env.get("AUTHENTIK_PF_PORT", "9001")
    fqdn = env.get(
        "AUTHENTIK_HOST_FQDN",
        "authentik-service.authentik-reusable-ml-services.svc.cluster.local",
    )
    return {
        "service_id": service_id,
        "namespace": env.get("NAMESPACE", service_id),
        "base_url": os.getenv("BASE_URL", f"http://localhost:{api_port}"),
        "auth_token_url": os.getenv(
            "AUTH_TOKEN_URL", f"http://localhost:{auth_port}/application/o/token/"
        ),
        "auth_client_id": env.get("OIDC_CLIENT_ID", f"{service_id}-api"),
        "auth_host_header": os.getenv("AUTH_HOST_HEADER", f"{fqdn}:9000"),
        "auth_client_secret": load_oidc_secret(service_id),
    }
