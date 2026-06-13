"""Render service artifacts from platform templates."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from ml_platform.generator.derive import build_context, load_yaml


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _template_env(repo_root: Path) -> Environment:
    loader = FileSystemLoader(str(repo_root / "ml_platform" / "templates"))
    return Environment(loader=loader, undefined=StrictUndefined, keep_trailing_newline=True)


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _render_template(env: Environment, template_name: str, ctx: dict) -> str:
    return env.get_template(template_name).render(**ctx)


def _compute_lock(service_yaml: Path, overlay_path: Path | None, env: str) -> str:
    h = hashlib.sha256()
    h.update(service_yaml.read_bytes())
    if overlay_path and overlay_path.is_file():
        h.update(overlay_path.read_bytes())
    h.update(env.encode())
    platform_dir = service_yaml.parents[2] / "ml_platform" / "templates"
    for path in sorted(platform_dir.rglob("*")):
        if path.is_file():
            h.update(path.read_bytes())
    return h.hexdigest()


def _shell_quote(value: str) -> str:
    if not value:
        return '""'
    if all(c.isalnum() or c in "._-:/@" for c in value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _write_deploy_env(out_dir: Path, ctx: dict) -> None:
    pairs = {
        "SERVICE_ID": ctx["service_id"],
        "APP_NAME": ctx["service_id"],
        "SERVICE_NAME": ctx["service_id"],
        "NAMESPACE": ctx["namespace"],
        "IMAGE_REPOSITORY": ctx["image_repository"],
        "IMAGE_NAME": ctx["image_name"],
        "BENTOML_SERVICE": ctx["bentoml_service"],
        "OIDC_APPLICATION_SLUG": ctx["oidc_application_slug"],
        "OIDC_CLIENT_ID": ctx["oidc_client_id"],
        "OIDC_PROVIDER_NAME": ctx["oidc_provider_name"],
        "OIDC_APP_DISPLAY_NAME": ctx["oidc_app_display_name"],
        "INFRA_NAMESPACE": ctx["infra_namespace"],
        "AUTHENTIK_HOST_FQDN": ctx["authentik_fqdn"],
        "MODEL_SOURCE": ctx["model_source"],
        "MODEL_ID": ctx["model_id"],
        "MODEL_REVISION": ctx["model_revision"],
        "MODEL_CACHE_DIR": ctx["model_cache_dir"],
        "PF_PORT": str(ctx["api_port"]),
        "AUTHENTIK_PF_PORT": str(ctx["authentik_port"]),
        "AUTHENTIK_PF_HTTPS_PORT": str(ctx["authentik_https_port"]),
        "GHCR_OWNER": ctx["ghcr_owner"],
        "REGISTRY": ctx["registry"],
        "IMAGE_PULL_POLICY": ctx["image_pull_policy"],
        "GENERATED_DIR": str(out_dir),
    }
    lines = [f"{key}={_shell_quote(str(value))}" for key, value in pairs.items()]
    _write_text(out_dir / "deploy.env", "\n".join(lines) + "\n")


def _write_bentoml_config(out_dir: Path, ctx: dict) -> None:
    doc = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": "bentoml-config",
            "labels": {
                "app.kubernetes.io/name": "bentoml",
                "app.kubernetes.io/component": "config",
            },
        },
        "data": {
            "REDIS_HOST": "redis-0.redis",
            "REDIS_PORT": "6379",
            "JOB_TTL": "3600",
            "MODEL_SOURCE": ctx["model_source"],
            "MODEL_ID": ctx["model_id"],
            "MODEL_REVISION": ctx["model_revision"],
            "MODEL_CACHE_DIR": ctx["model_cache_dir"],
            "MODEL_LOCAL_FILES_ONLY": "true",
        },
    }
    _write_text(out_dir / "bentoml-config.yaml", yaml.safe_dump(doc, sort_keys=False))


def _write_secrets_example(out_dir: Path, ctx: dict) -> None:
    doc = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": "app-secrets",
            "labels": {
                "app.kubernetes.io/name": "model-api",
                "app.kubernetes.io/component": "secrets",
            },
        },
        "type": "Opaque",
        "stringData": {
            "REDIS_PASSWORD": "CHANGE_ME_REDIS_PASSWORD",
            "POSTGRES_PASSWORD": "CHANGE_ME_POSTGRES_PASSWORD",
            "KRAKEND_DB_PASSWORD": "CHANGE_ME_KRAKEND_DB_PASSWORD",
            "oidc-client-id": ctx["oidc_client_id"],
            "oidc-client-secret": "CHANGE_ME_OIDC_CLIENT_SECRET",
        },
    }
    _write_text(out_dir / "secrets.yaml.example", yaml.safe_dump(doc, sort_keys=False))


def _write_apply_order(out_dir: Path, ctx: dict, env: str) -> None:
    ns = ctx["namespace"]
    lines = [
        f"# Generated apply order for {ctx['service_id']} ({env})",
        f"export NS={ns}",
        "",
        "# 1. Secrets and BentoML config (use secrets.local.yaml outside repo in prod)",
        f"kubectl apply -n \"$NS\" -f secrets.local.yaml  # or secrets.yaml.example filled",
        f"kubectl apply -n \"$NS\" -f bentoml-config.yaml",
        "",
        "# 2. KrakenD ConfigMaps — from repo root:",
        f"#    k8s/render-gateway-configmaps.sh --namespace $NS --gateway-settings gateway-settings.json --service-name {ctx['service_id']}",
        "",
        "# 3. Data layer",
        "kubectl apply -n \"$NS\" -f k8s/01-redis.yaml",
        "kubectl rollout status statefulset/redis -n \"$NS\" --timeout=120s",
        "kubectl apply -n \"$NS\" -f k8s/03-postgresql.yaml",
        "kubectl rollout status statefulset/postgresql -n \"$NS\" --timeout=120s",
        "",
        "# 4. Gateway + inference",
        "kubectl apply -n \"$NS\" -f k8s/04-krakend.yaml",
        "kubectl rollout status deployment/krakend -n \"$NS\" --timeout=300s",
        "kubectl apply -n \"$NS\" -f k8s/02-bentoml.yaml",
        "kubectl rollout status deployment/bentoml -n \"$NS\" --timeout=600s",
        "",
        "# 5. Autoscaling + ingress",
        "kubectl apply -n \"$NS\" -f k8s/07-hpa.yaml",
    ]
    if ctx["ingress"]["enabled"]:
        lines.append("kubectl apply -n \"$NS\" -f k8s/05-ingress.yaml")
    _write_text(out_dir / "apply-order.txt", "\n".join(lines) + "\n")


def _write_preflight(out_dir: Path, ctx: dict, env: str) -> None:
    content = f"""# Preflight checklist — {ctx['service_id']} ({env})

## Before apply

- [ ] Run `make verify-prod SERVICE={ctx['service_id']}` (prod only)
- [ ] Secrets filled (no CHANGE_ME values)
- [ ] Gateway settings have no REPLACE_WITH placeholders (prod)
- [ ] Image available: `{ctx['image_name']}`

## After apply

- [ ] `kubectl get pods -n {ctx['namespace']}` — all Ready
- [ ] `curl -fsS http://localhost:{ctx['api_port']}/__health` (dev port-forward)
- [ ] `./k8s/app.sh --service {ctx['service_id']} token` returns JWT (dev)
- [ ] `make test-service SERVICE={ctx['service_id']}` passes (dev)
"""
    _write_text(out_dir / "preflight-checklist.md", content)


def render_service(service_id: str, env: str) -> Path:
    repo_root = _repo_root()
    service_dir = repo_root / "services" / service_id
    service_yaml = service_dir / "service.yaml"
    overlay_path = service_dir / "prod.overlay.yaml"
    overlay_example = service_dir / "prod.overlay.yaml.example"

    overlay: dict = {}
    if env == "prod":
        if overlay_path.is_file():
            overlay = load_yaml(overlay_path)
        elif overlay_example.is_file():
            overlay = load_yaml(overlay_example)
        else:
            raise FileNotFoundError(
                f"Prod render requires {overlay_path} or {overlay_example}"
            )

    ctx = build_context(repo_root, service_id, env, overlay)
    ctx["image_tag"] = "latest"
    if env == "prod":
        image_cfg = overlay.get("image") or {}
        ctx["image_tag"] = image_cfg.get("tag", "latest")

    out_dir = service_dir / "generated" / env
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    k8s_out = out_dir / "k8s"
    k8s_out.mkdir()

    jinja = _template_env(repo_root)
    for template_path in sorted((repo_root / "ml_platform" / "templates" / "k8s" / "app").glob("*.j2")):
        name = template_path.name
        if name == "00-namespace.yaml.j2":
            continue
        if name == "05-ingress.dev.yaml.j2" and env != "dev":
            continue
        if name == "05-ingress.yaml.j2" and env == "dev":
            continue
        rendered = _render_template(jinja, f"k8s/app/{name}", ctx)
        _write_text(k8s_out / name.replace(".j2", ""), rendered)

    if env == "prod" and ctx["ingress"]["enabled"]:
        _write_text(k8s_out / "05-ingress.yaml", _render_template(jinja, "k8s/app/05-ingress.yaml.j2", ctx))

    gs = ctx["gateway_settings"]
    _write_text(out_dir / "gateway-settings.json", json.dumps(gs, indent=2) + "\n")
    if env == "dev":
        _write_text(
            out_dir / "gateway-settings.dev-workaround.json",
            json.dumps(gs, indent=2) + "\n",
        )

    _write_bentoml_config(out_dir, ctx)
    _write_secrets_example(out_dir, ctx)
    _write_deploy_env(out_dir, ctx)
    _write_apply_order(out_dir, ctx, env)
    _write_preflight(out_dir, ctx, env)

    lock = _compute_lock(service_yaml, overlay_path if overlay_path.is_file() else None, env)
    _write_text(out_dir / "service.lock", lock + "\n")

    return out_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render service deployment artifacts")
    parser.add_argument("--service", required=True)
    parser.add_argument("--env", choices=["dev", "prod"], default="dev")
    args = parser.parse_args(argv)

    out = render_service(args.service, args.env)
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
