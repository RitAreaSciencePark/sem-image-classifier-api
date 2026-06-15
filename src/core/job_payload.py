"""Job payload encoding for Redis queue (URL deferral vs inline image bytes)."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

PAYLOAD_VERSION = 1


def validate_image_url(image_url: str) -> None:
    parsed = urlparse(image_url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"Invalid URL scheme '{parsed.scheme}'. Only http/https allowed."
        )
    if not parsed.netloc:
        raise ValueError(f"Invalid URL: {image_url}")


def encode_url_payload(image_url: str) -> str:
    validate_image_url(image_url)
    return json.dumps({"v": PAYLOAD_VERSION, "kind": "url", "url": image_url})


def encode_image_payload(image_b64: str) -> str:
    return json.dumps({"v": PAYLOAD_VERSION, "kind": "image_b64", "data": image_b64})


def decode_payload_kind(payload: str) -> tuple[str, dict[str, Any]]:
    """Return (kind, data). Supports legacy raw base64 PNG payloads."""
    stripped = payload.strip()
    if stripped.startswith("{"):
        data = json.loads(stripped)
        if not isinstance(data, dict):
            raise ValueError("JSON payload must be an object")
        kind = str(data.get("kind", ""))
        if kind not in ("url", "image_b64"):
            raise ValueError(f"Unsupported payload kind: {kind!r}")
        return kind, data
    return "legacy_image_b64", {"data": payload}
