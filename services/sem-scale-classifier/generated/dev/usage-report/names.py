"""Filename helpers for usage report artifacts."""

from __future__ import annotations

import re
from datetime import datetime, timezone

_UNSAFE = re.compile(r"[/\\ :]+")


def sanitize_label(value: str) -> str:
    """Make a CLI value safe for filesystem names."""
    return _UNSAFE.sub("-", value.strip()) or "unknown"


def report_filename(
    *,
    namespace: str,
    since: str,
    until: str,
    bucket: str,
    timezone_name: str,
    fmt: str,
    generated_at: datetime | None = None,
) -> str:
    """Build a unique, parameter-encoded report filename."""
    stamp = (generated_at or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    ext_map = {"json": "json", "summary": "txt", "html": "html"}
    ext = ext_map.get(fmt, "json")
    parts = [
        "api-usage",
        sanitize_label(namespace),
        f"since-{sanitize_label(since)}",
        f"until-{sanitize_label(until)}",
        f"bucket-{sanitize_label(bucket)}",
        f"tz-{sanitize_label(timezone_name)}",
        stamp,
    ]
    return "_".join(parts) + f".{ext}"
