"""Unit tests for usage report SQL, names, and rendering."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from ml_platform.usage_report.names import report_filename, sanitize_label
from ml_platform.usage_report.sql import build_sql, choose_bucket, time_expr
from scripts.reports import usage_report as ur


def test_time_expr_relative_hours() -> None:
    assert "interval '24 hours'" in time_expr("24h", default_now=False)


def test_choose_bucket_light_window() -> None:
    assert choose_bucket("summary", "2h", "auto") == "minute"


def test_build_sql_contains_api_usage_and_meta() -> None:
    sql = build_sql(
        since="24h",
        until=None,
        timezone_name="UTC",
        bucket="hour",
        recent_limit=10,
        namespace="prod-ns",
        since_label="24h",
        until_label="now",
    )
    assert "FROM api_usage" in sql
    assert "report_meta" in sql
    assert "'prod-ns'" in sql


def test_report_filename_encodes_params() -> None:
    name = report_filename(
        namespace="sem-classifier",
        since="30d",
        until="now",
        bucket="hour",
        timezone_name="UTC",
        fmt="json",
        generated_at=__import__("datetime").datetime(2026, 6, 7, 14, 30, 22, tzinfo=__import__("datetime").timezone.utc),
    )
    assert name == "api-usage_sem-classifier_since-30d_until-now_bucket-hour_tz-UTC_20260607T143022Z.json"


def test_report_filename_html() -> None:
    name = report_filename(
        namespace="sem-classifier",
        since="24h",
        until="now",
        bucket="hour",
        timezone_name="UTC",
        fmt="html",
        generated_at=__import__("datetime").datetime(2026, 6, 7, 14, 30, 22, tzinfo=__import__("datetime").timezone.utc),
    )
    assert name.endswith(".html")
    assert "since-24h" in name


def test_sanitize_label() -> None:
    assert sanitize_label("a/b c") == "a-b-c"


def test_run_query_parses_json() -> None:
    payload = {"total": 3, "report_meta": {"namespace": "ns"}}
    proc = type("Proc", (), {"returncode": 0, "stdout": json.dumps(payload), "stderr": ""})()
    with patch("scripts.reports.usage_report.subprocess.run", return_value=proc):
        assert ur.run_query("ns", "SELECT 1")["total"] == 3


def test_run_query_raises_on_failure() -> None:
    proc = type("Proc", (), {"returncode": 1, "stdout": "", "stderr": "boom"})()
    with patch("scripts.reports.usage_report.subprocess.run", return_value=proc):
        with pytest.raises(RuntimeError, match="boom"):
            ur.run_query("ns", "SELECT 1")


def test_render_summary_shows_namespace() -> None:
    text = ur.render_summary(
        {"total": 0, "error_count": 0, "unique_users": 0, "report_meta": {"since": "24h"}},
        namespace="prod-ns",
    )
    assert "Namespace: prod-ns" in text
    assert "Since: 24h" in text


def test_parse_args_requires_namespace_for_live() -> None:
    with pytest.raises(SystemExit):
        ur.parse_args(["summary"])


def test_parse_args_input_mode() -> None:
    args = ur.parse_args(["summary", "--input", "/tmp/x.json"])
    assert args.input == "/tmp/x.json"
    assert args.namespace == ""


def test_parse_args_defaults_since() -> None:
    args = ur.parse_args(["summary", "--namespace", "x"])
    assert args.namespace == "x"
    assert args.since == "24h"


def test_namespace_from_report_meta() -> None:
    assert ur.namespace_from_report({"report_meta": {"namespace": "abc"}}) == "abc"
