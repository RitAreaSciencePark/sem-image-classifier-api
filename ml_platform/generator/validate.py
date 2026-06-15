"""Validate service.yaml and model imports."""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

import yaml


def validate_service(service_id: str) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    service_yaml = repo_root / "services" / service_id / "service.yaml"
    if not service_yaml.is_file():
        raise FileNotFoundError(service_yaml)

    data = yaml.safe_load(service_yaml.read_text(encoding="utf-8")) or {}
    required = ["service_id", "display_name", "model"]
    for key in required:
        if key not in data:
            raise ValueError(f"Missing required key in service.yaml: {key}")

    k8s = data.get("kubernetes") or {}
    if not k8s.get("namespace"):
        raise ValueError("Missing kubernetes.namespace in service.yaml (dev cluster namespace)")

    dev_access = data.get("dev_access") or {}
    for key in ("api_port", "authentik_port", "authentik_https_port"):
        if key not in dev_access:
            raise ValueError(f"Missing dev_access.{key} in service.yaml")

    model = data["model"]
    for key in ("module", "class", "bento_name", "source", "id"):
        if key not in model:
            raise ValueError(f"Missing model.{key} in service.yaml")

    module_path = repo_root / "src" / f"{model['module'].replace('.', '/')}.py"
    if not module_path.is_file():
        raise FileNotFoundError(f"Model module not found: {module_path}")

    import ast

    ast.parse(module_path.read_text(encoding="utf-8"))
    source = module_path.read_text(encoding="utf-8")
    if f"class {model['class']}" not in source:
        raise AttributeError(f"{model['class']} not found in {module_path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--service", required=True)
    args = parser.parse_args(argv)
    validate_service(args.service)
    print(f"OK: {args.service}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
