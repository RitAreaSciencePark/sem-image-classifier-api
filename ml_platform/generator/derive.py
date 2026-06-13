"""Derive deployment context from service.yaml + environment."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def _slug_to_api_client(service_id: str) -> str:
    return f"{service_id}-api"


def _module_to_bentoml_service(module: str, class_name: str) -> str:
    return f"{module}:{class_name}"


def load_yaml(path: Path) -> dict[str, Any]:
    import yaml

    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Expected mapping in {path}")
    return data


def load_platform_config(repo_root: Path) -> dict[str, Any]:
    path = repo_root / "ml_platform" / "config.yaml"
    if not path.is_file():
        return {
            "infra_namespace": "authentik-reusable-ml-services",
            "ghcr_owner": "luisfpal",
            "registry_scheme": "https",
        }
    return load_yaml(path)


def build_context(
    repo_root: Path,
    service_id: str,
    env: str,
    overlay: dict[str, Any] | None = None,
) -> dict[str, Any]:
    service_path = repo_root / "services" / service_id / "service.yaml"
    if not service_path.is_file():
        raise FileNotFoundError(f"Missing service definition: {service_path}")

    service = load_yaml(service_path)
    if service.get("service_id") != service_id:
        raise ValueError(
            f"service_id mismatch: directory {service_id} vs yaml {service.get('service_id')}"
        )

    platform = load_platform_config(repo_root)
    overlay = overlay or {}
    model = service.get("model") or {}
    dev_access = service.get("dev_access") or {}
    oidc = service.get("oidc") or {}

    namespace = service_id
    ghcr_owner = platform.get("ghcr_owner", "luisfpal")
    image_tag = "latest"
    image_digest = ""

    if env == "prod":
        k8s_overlay = overlay.get("kubernetes") or {}
        namespace = k8s_overlay.get("namespace") or service_id
        image_cfg = overlay.get("image") or {}
        ghcr_owner = (overlay.get("registry") or {}).get("ghcr_owner") or ghcr_owner
        image_tag = image_cfg.get("tag") or image_tag
        image_digest = image_cfg.get("digest") or ""

    image_repository = service_id
    registry = f"ghcr.io/{ghcr_owner}"
    base = f"{registry}/{image_repository}"
    if image_digest and image_digest.startswith("sha256:"):
        image_name = f"{base}@{image_digest}"
    else:
        image_name = f"{base}:{image_tag}"
    image_pull_policy = "Always"

    application_slug = oidc.get("application_slug") or service_id
    client_id = oidc.get("client_id") or _slug_to_api_client(service_id)
    provider_name = oidc.get("provider_name") or f"{service_id}-provider"
    app_display_name = oidc.get("app_display_name") or service.get("display_name") or service_id

    module = model.get("module", "")
    class_name = model.get("class", "")
    bentoml_service = _module_to_bentoml_service(module, class_name)

    infra_ns = platform.get("infra_namespace", "authentik-reusable-ml-services")
    authentik_fqdn = f"authentik-service.{infra_ns}.svc.cluster.local"

    ctx: dict[str, Any] = {
        "env": env,
        "service_id": service_id,
        "display_name": service.get("display_name") or service_id,
        "namespace": namespace,
        "image_repository": image_repository,
        "image_name": image_name,
        "registry": registry,
        "image_pull_policy": image_pull_policy,
        "ghcr_owner": ghcr_owner,
        "bentoml_service": bentoml_service,
        "bento_name": model.get("bento_name") or service_id,
        "model_module": module,
        "model_class": class_name,
        "model_source": model.get("source", "hugging_face"),
        "model_id": model.get("id", ""),
        "model_revision": model.get("revision", ""),
        "model_cache_dir": model.get("cache_dir", ""),
        "oidc_application_slug": application_slug,
        "oidc_client_id": client_id,
        "oidc_provider_name": provider_name,
        "oidc_app_display_name": app_display_name,
        "infra_namespace": infra_ns,
        "authentik_fqdn": authentik_fqdn,
        "dev_access": dev_access,
        "api_port": dev_access.get("api_port", 8080),
        "authentik_port": dev_access.get("authentik_port", 9001),
        "authentik_https_port": dev_access.get("authentik_https_port", 9443),
    }

    if env == "dev":
        ctx["gateway_settings"] = _dev_gateway_settings(ctx)
    else:
        ctx["gateway_settings"] = _prod_gateway_settings(ctx, overlay)

    ctx["ingress"] = _ingress_context(env, service_id, overlay)
    return ctx


def _dev_gateway_settings(ctx: dict[str, Any]) -> dict[str, Any]:
    slug = ctx["oidc_application_slug"]
    fqdn = ctx["authentik_fqdn"]
    base = f"http://{fqdn}:9000/application/o/{slug}"
    return {
        "port": 8080,
        "timeout": "30s",
        "backend_host": "http://bentoml:3000",
        "service_name": ctx["service_id"],
        "display_name": ctx["display_name"],
        "auth": {
            "provider": "authentik",
            "alg": "RS256",
            "jwk_url": f"{base}/jwks/",
            "issuer": f"{base}/",
            "audience": ctx["oidc_client_id"],
            "disable_jwk_security": True,
            "operation_debug": False,
            "oidc_application_slug": slug,
            "oidc_client_id": ctx["oidc_client_id"],
        },
        "plugin": {
            "pattern": ".so",
            "folder": "/etc/krakend/plugins",
            "paths": [
                "/api/v1/inference",
                "/api/v1/jobs/status",
                "/api/v1/jobs/results",
            ],
        },
        "database": {
            "host": "postgresql-0.postgresql",
            "port": "5432",
            "user": "krakend",
            "password": "",
            "dbname": "krakend",
        },
    }


def _prod_gateway_settings(ctx: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    auth = overlay.get("auth") or {}
    slug = ctx["oidc_application_slug"]
    return {
        "port": 8080,
        "timeout": "30s",
        "backend_host": "http://bentoml:3000",
        "service_name": ctx["service_id"],
        "display_name": ctx["display_name"],
        "auth": {
            "provider": "authentik",
            "alg": "RS256",
            "jwk_url": auth.get("jwk_url", "REPLACE_WITH_HTTPS_JWKS_URL"),
            "issuer": auth.get("issuer", "REPLACE_WITH_HTTPS_ISSUER"),
            "audience": ctx["oidc_client_id"],
            "disable_jwk_security": auth.get("disable_jwk_security", False),
            "operation_debug": auth.get("operation_debug", False),
            "oidc_application_slug": slug,
            "oidc_client_id": ctx["oidc_client_id"],
        },
        "plugin": {
            "pattern": ".so",
            "folder": "/etc/krakend/plugins",
            "paths": [
                "/api/v1/inference",
                "/api/v1/jobs/status",
                "/api/v1/jobs/results",
            ],
        },
        "database": {
            "host": "postgresql-0.postgresql",
            "port": "5432",
            "user": "krakend",
            "password": "",
            "dbname": "krakend",
        },
    }


def _ingress_context(env: str, service_id: str, overlay: dict[str, Any]) -> dict[str, Any]:
    if env == "dev":
        return {"enabled": False, "host": "", "ingress_class": "haproxy-4"}
    ing = overlay.get("ingress") or {}
    return {
        "enabled": ing.get("enabled", True),
        "host": ing.get("host", f"REPLACE_WITH_{service_id.upper()}_HOST"),
        "ingress_class": ing.get("ingress_class", "haproxy-4"),
    }
