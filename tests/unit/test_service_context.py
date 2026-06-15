"""Unit tests for service context loading."""

from __future__ import annotations

import ml_platform.devtools.service_context as service_context
from ml_platform.devtools.service_context import load_deploy_env, load_oidc_secret, load_service_context


def test_load_deploy_env_parses_key_values(tmp_path, monkeypatch) -> None:
    service_id = "demo-svc"
    deploy_dir = tmp_path / "services" / service_id / "generated" / "dev"
    deploy_dir.mkdir(parents=True)
    (deploy_dir / "deploy.env").write_text(
        "PF_PORT=9090\nNAMESPACE=demo-ns\nOIDC_CLIENT_ID=demo-api\n"
    )
    monkeypatch.setattr(service_context, "repo_root", lambda: tmp_path)

    env = load_deploy_env(service_id)

    assert env["PF_PORT"] == "9090"
    assert env["NAMESPACE"] == "demo-ns"
    assert env["OIDC_CLIENT_ID"] == "demo-api"


def test_load_service_context_uses_defaults_when_deploy_env_missing(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(service_context, "repo_root", lambda: tmp_path)
    monkeypatch.delenv("BASE_URL", raising=False)
    monkeypatch.delenv("AUTH_TOKEN_URL", raising=False)

    ctx = load_service_context("missing-svc")

    assert ctx.service_id == "missing-svc"
    assert ctx.namespace == "missing-svc"
    assert ctx.base_url == "http://localhost:8080"
    assert ctx.auth_client_id == "missing-svc-api"


def test_load_oidc_secret_from_yaml(tmp_path, monkeypatch) -> None:
    service_id = "demo-svc"
    secrets_dir = tmp_path / "services" / service_id
    secrets_dir.mkdir(parents=True)
    (secrets_dir / "secrets.local.yaml").write_text(
        'oidc-client-secret: "super-secret"\n'
    )
    monkeypatch.setattr(service_context, "repo_root", lambda: tmp_path)
    monkeypatch.delenv("AUTH_CLIENT_SECRET", raising=False)

    assert load_oidc_secret(service_id) == "super-secret"
