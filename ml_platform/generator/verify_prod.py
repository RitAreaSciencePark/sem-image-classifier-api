"""Production bundle preflight checks."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path



def _validate_prod_overlay(service_id: str, repo_root: Path) -> list[str]:
    """Structural checks for prod.overlay.yaml (matches prod-overlay.schema.json)."""
    errors: list[str] = []
    overlay_path = repo_root / "services" / service_id / "prod.overlay.yaml"
    example_path = repo_root / "services" / service_id / "prod.overlay.yaml.example"
    path = overlay_path if overlay_path.is_file() else example_path
    if not path.is_file():
        errors.append(f"Missing prod.overlay.yaml or .example for {service_id}")
        return errors

    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    for key in ("kubernetes", "auth", "ingress", "image", "registry"):
        if key not in data:
            errors.append(f"{path.name}: missing required key '{key}'")

    auth = data.get("auth") or {}
    for key in ("jwk_url", "issuer"):
        val = str(auth.get(key, ""))
        if not val.startswith("https://"):
            errors.append(f"{path.name}: auth.{key} must start with https://")
        if "REPLACE_WITH" in val:
            errors.append(f"{path.name}: auth.{key} still has placeholder")

    if auth.get("disable_jwk_security") is True:
        errors.append(f"{path.name}: auth.disable_jwk_security must be false")
    if auth.get("operation_debug") is True:
        errors.append(f"{path.name}: auth.operation_debug must be false")

    return errors


def verify_prod(service_id: str) -> list[str]:
    repo_root = Path(__file__).resolve().parents[2]
    prod_dir = repo_root / "services" / service_id / "generated" / "prod"
    errors: list[str] = []

    errors.extend(_validate_prod_overlay(service_id, repo_root))

    if not prod_dir.is_dir():
        errors.append(f"Missing generated prod dir: {prod_dir} (run make render-prod)")
        return errors

    for path in prod_dir.rglob("*"):
        if path.is_file() and path.suffix in {".yaml", ".json", ".txt", ".md"}:
            text = path.read_text(encoding="utf-8", errors="replace")
            if "REPLACE_WITH_" in text:
                errors.append(f"Placeholder REPLACE_WITH_ in {path.relative_to(repo_root)}")

    gs_path = prod_dir / "gateway-settings.json"
    if gs_path.is_file():
        gs = json.loads(gs_path.read_text(encoding="utf-8"))
        auth = gs.get("auth") or {}
        for key in ("jwk_url", "issuer"):
            val = auth.get(key, "")
            if not str(val).startswith("https://"):
                errors.append(f"auth.{key} must be https:// in prod gateway-settings")
        if auth.get("disable_jwk_security") is True:
            errors.append("auth.disable_jwk_security must be false in prod")
        if auth.get("operation_debug") is True:
            errors.append("auth.operation_debug must be false in prod")

    lock_path = prod_dir / "service.lock"
    if not lock_path.is_file():
        errors.append("Missing service.lock — re-run make render-prod")

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--service", required=True)
    args = parser.parse_args(argv)
    errors = verify_prod(args.service)
    if errors:
        for err in errors:
            print(f"FAIL: {err}", file=sys.stderr)
        return 1
    print(f"OK: prod bundle for {args.service}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
