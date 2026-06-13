"""Scaffold a new service definition and model stub."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml


def _snake(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def scaffold(service_id: str, model_id: str, model_source: str, api_port: int, authentik_port: int) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    service_dir = repo_root / "services" / service_id
    if service_dir.exists():
        raise FileExistsError(f"Service already exists: {service_dir}")

    module = f"models.{_snake(service_id)}"
    class_name = "".join(part.capitalize() for part in service_id.replace("-", "_").split("_")) + "Service"

    service_dir.mkdir(parents=True)
    service_yaml = {
        "service_id": service_id,
        "display_name": service_id.replace("-", " ").title() + " API",
        "model": {
            "module": module,
            "class": class_name,
            "bento_name": service_id,
            "source": model_source,
            "id": model_id,
            "revision": "",
            "cache_dir": "",
        },
        "dev_access": {
            "api_port": api_port,
            "authentik_port": authentik_port,
            "authentik_https_port": authentik_port + 42,
        },
    }
    (service_dir / "service.yaml").write_text(
        yaml.safe_dump(service_yaml, sort_keys=False), encoding="utf-8"
    )

    overlay_example = {
        "kubernetes": {"namespace": service_id},
        "auth": {
            "mode": "external",
            "jwk_url": "REPLACE_WITH_HTTPS_JWKS_URL",
            "issuer": "REPLACE_WITH_HTTPS_ISSUER",
            "disable_jwk_security": False,
            "operation_debug": False,
        },
        "ingress": {
            "enabled": True,
            "host": f"REPLACE_WITH_{service_id.upper().replace('-', '_')}_HOST",
            "ingress_class": "haproxy-4",
        },
        "image": {"registry": "ghcr.io/luisfpal", "tag": "latest", "digest": ""},
        "registry": {"ghcr_owner": "luisfpal"},
    }
    (service_dir / "prod.overlay.yaml.example").write_text(
        "# yaml-language-server: $schema=../../ml_platform/schemas/prod-overlay.schema.json\n"
        + yaml.safe_dump(overlay_example, sort_keys=False),
        encoding="utf-8",
    )

    model_path = repo_root / "src" / "models" / f"{_snake(service_id)}.py"
    model_path.parent.mkdir(parents=True, exist_ok=True)
    if not model_path.exists():
        model_path.write_text(
            f'''"""BentoML service for {service_id}."""

import bentoml

from core.image_classification_service import ImageClassificationModelService


@bentoml.service(name="{service_id}", traffic={{"timeout": 300}})
class {class_name}(ImageClassificationModelService):
    """Image classification model service."""
''',
            encoding="utf-8",
        )

    print(f"Scaffolded {service_dir} and {model_path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--service", required=True)
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--model-source", default="hugging_face")
    parser.add_argument("--api-port", type=int, default=8080)
    parser.add_argument("--authentik-port", type=int, default=9001)
    args = parser.parse_args(argv)
    scaffold(args.service, args.model_id, args.model_source, args.api_port, args.authentik_port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
