"""Unit tests for job payload encoding."""

from __future__ import annotations

import json

import pytest

from src.core.job_payload import (
    decode_payload_kind,
    encode_image_payload,
    encode_url_payload,
    validate_image_url,
)


def test_encode_url_payload() -> None:
    payload = encode_url_payload("https://example.com/a.jpg")
    data = json.loads(payload)
    assert data["kind"] == "url"
    assert data["url"] == "https://example.com/a.jpg"


def test_encode_image_payload() -> None:
    payload = encode_image_payload("abc123")
    data = json.loads(payload)
    assert data["kind"] == "image_b64"
    assert data["data"] == "abc123"


def test_decode_legacy_base64() -> None:
    kind, data = decode_payload_kind("legacydata")
    assert kind == "legacy_image_b64"
    assert data["data"] == "legacydata"


def test_validate_image_url_rejects_bad_scheme() -> None:
    with pytest.raises(ValueError, match="scheme"):
        validate_image_url("ftp://example.com/x.jpg")
