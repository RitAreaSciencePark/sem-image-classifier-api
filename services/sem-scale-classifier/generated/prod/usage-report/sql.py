"""SQL builder for api_usage reports."""

from __future__ import annotations

import re

RELATIVE_RE = re.compile(r"^(\d+)(s|m|h|d|w)$")

REPORT_VERSION = "2"
GENERATOR = "usage-report/run.sh"

# Placeholders substituted by run.sh at runtime (see build_sql_template).
START_EXPR = "__START_EXPR__"
END_EXPR = "__END_EXPR__"
TZ_LITERAL = "__TZ__"
BUCKET_LITERAL = "__BUCKET__"
RECENT_LIMIT = "__RECENT_LIMIT__"
META_NAMESPACE = "__META_NAMESPACE__"
META_SINCE = "__META_SINCE__"
META_UNTIL = "__META_UNTIL__"
META_POSTGRES_POD = "__META_POSTGRES_POD__"
META_KUBE_CONTEXT = "__META_KUBE_CONTEXT__"


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def time_expr(value: str | None, *, default_now: bool) -> str:
    if not value:
        return "NOW()" if default_now else "NULL::timestamptz"
    normalized = value.strip().lower()
    if normalized in {"now", "all"}:
        return "NOW()" if default_now else "NULL::timestamptz"
    match = RELATIVE_RE.match(normalized)
    if match:
        amount, unit = match.groups()
        unit_name = {
            "s": "seconds",
            "m": "minutes",
            "h": "hours",
            "d": "days",
            "w": "weeks",
        }[unit]
        return f"NOW() - interval {sql_literal(amount + ' ' + unit_name)}"
    return f"{sql_literal(value)}::timestamptz"


def choose_bucket(mode: str, since: str | None, bucket: str) -> str:
    if bucket != "auto":
        return bucket
    if mode == "report" and not since:
        return "day"
    if since:
        match = RELATIVE_RE.match(since.strip().lower())
        if match:
            amount, unit = match.groups()
            seconds = (
                int(amount)
                * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]
            )
            if seconds <= 6 * 3600:
                return "minute"
            if seconds <= 7 * 86400:
                return "hour"
    return "day" if mode == "report" else "hour"


def _report_body(
    *,
    start_expr: str,
    end_expr: str,
    tz: str,
    bucket_literal: str,
    recent_limit: str,
    meta_namespace: str,
    meta_since: str,
    meta_until: str,
    meta_postgres_pod: str,
    meta_kube_context: str,
) -> str:
    return f"""
WITH bounds AS (
    SELECT {start_expr} AS start_at, {end_expr} AS end_at
),
base AS (
    SELECT
        id,
        "timestamp",
        COALESCE(NULLIF(username, ''), '<unknown>') AS username,
        COALESCE(NULLIF(service_name, ''), '<unknown>') AS service_name,
        COALESCE(NULLIF(endpoint_type, ''), '<unknown>') AS endpoint_type,
        status_code,
        url_path
    FROM api_usage, bounds
    WHERE (bounds.start_at IS NULL OR "timestamp" >= bounds.start_at)
      AND "timestamp" <= bounds.end_at
),
endpoint_counts AS (
    SELECT endpoint_type AS name, COUNT(*)::int AS n
    FROM base GROUP BY endpoint_type ORDER BY n DESC, name ASC
),
status_counts AS (
    SELECT COALESCE(status_code::text, '<null>') AS name, COUNT(*)::int AS n
    FROM base GROUP BY status_code ORDER BY name ASC
),
user_counts AS (
    SELECT username AS name, COUNT(*)::int AS n
    FROM base GROUP BY username ORDER BY n DESC, name ASC LIMIT 25
),
path_counts AS (
    SELECT url_path AS name, COUNT(*)::int AS n
    FROM base GROUP BY url_path ORDER BY n DESC, name ASC LIMIT 25
),
time_buckets AS (
    SELECT
        date_trunc({bucket_literal}, "timestamp" AT TIME ZONE {tz}) AS bucket_at,
        COUNT(*)::int AS n,
        COUNT(*) FILTER (WHERE status_code >= 400)::int AS errors
    FROM base GROUP BY bucket_at ORDER BY bucket_at ASC
),
hour_heatmap AS (
    SELECT
        EXTRACT(ISODOW FROM "timestamp" AT TIME ZONE {tz})::int AS dow,
        EXTRACT(HOUR FROM "timestamp" AT TIME ZONE {tz})::int AS hour,
        COUNT(*)::int AS n
    FROM base GROUP BY dow, hour ORDER BY dow, hour
),
recent AS (
    SELECT id, "timestamp", username, service_name, endpoint_type, status_code, url_path
    FROM base ORDER BY "timestamp" DESC LIMIT {recent_limit}
)
SELECT json_build_object(
    'report_meta', json_build_object(
        'report_version', {sql_literal(REPORT_VERSION)},
        'generator', {sql_literal(GENERATOR)},
        'namespace', {meta_namespace},
        'since', {meta_since},
        'until', {meta_until},
        'bucket', {bucket_literal},
        'timezone', {tz},
        'postgres_pod', {meta_postgres_pod},
        'kube_context', {meta_kube_context},
        'generated_at_utc', to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')
    ),
    'generated_at_utc', to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
    'timezone', {tz},
    'bucket', {bucket_literal},
    'requested_start_utc', (SELECT CASE WHEN start_at IS NULL THEN NULL ELSE to_char(start_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') END FROM bounds),
    'requested_end_utc', (SELECT to_char(end_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') FROM bounds),
    'coverage_start', (SELECT to_char(MIN("timestamp") AT TIME ZONE {tz}, 'YYYY-MM-DD HH24:MI:SS') FROM base),
    'coverage_end', (SELECT to_char(MAX("timestamp") AT TIME ZONE {tz}, 'YYYY-MM-DD HH24:MI:SS') FROM base),
    'total', (SELECT COUNT(*)::int FROM base),
    'unique_users', (SELECT COUNT(DISTINCT username)::int FROM base),
    'error_count', (SELECT COUNT(*) FILTER (WHERE status_code >= 400)::int FROM base),
    'endpoint_counts', COALESCE((SELECT json_agg(json_build_object('name', name, 'n', n) ORDER BY n DESC, name ASC) FROM endpoint_counts), '[]'::json),
    'status_counts', COALESCE((SELECT json_agg(json_build_object('name', name, 'n', n) ORDER BY name ASC) FROM status_counts), '[]'::json),
    'user_counts', COALESCE((SELECT json_agg(json_build_object('name', name, 'n', n) ORDER BY n DESC, name ASC) FROM user_counts), '[]'::json),
    'path_counts', COALESCE((SELECT json_agg(json_build_object('name', name, 'n', n) ORDER BY n DESC, name ASC) FROM path_counts), '[]'::json),
    'time_buckets', COALESCE((SELECT json_agg(json_build_object(
        'bucket', to_char(bucket_at, 'YYYY-MM-DD HH24:MI:SS'), 'n', n, 'errors', errors
    ) ORDER BY bucket_at ASC) FROM time_buckets), '[]'::json),
    'hour_heatmap', COALESCE((SELECT json_agg(json_build_object('dow', dow, 'hour', hour, 'n', n) ORDER BY dow, hour) FROM hour_heatmap), '[]'::json),
    'recent', COALESCE((SELECT json_agg(json_build_object(
        'id', id,
        'timestamp', to_char("timestamp" AT TIME ZONE {tz}, 'YYYY-MM-DD HH24:MI:SS'),
        'username', username,
        'service_name', service_name,
        'endpoint_type', endpoint_type,
        'status_code', status_code,
        'url_path', url_path
    ) ORDER BY "timestamp" DESC) FROM recent), '[]'::json)
) AS report;
""".strip()


def build_sql(
    *,
    since: str | None,
    until: str | None,
    timezone_name: str,
    bucket: str,
    recent_limit: int,
    namespace: str = "",
    since_label: str | None = None,
    until_label: str | None = None,
    postgres_pod: str = "postgresql-0",
    kube_context: str = "",
) -> str:
    since_label = since_label if since_label is not None else (since or "all")
    until_label = until_label if until_label is not None else (until or "now")
    return _report_body(
        start_expr=time_expr(since, default_now=False),
        end_expr=time_expr(until, default_now=True),
        tz=sql_literal(timezone_name),
        bucket_literal=sql_literal(bucket),
        recent_limit=str(recent_limit),
        meta_namespace=sql_literal(namespace),
        meta_since=sql_literal(since_label),
        meta_until=sql_literal(until_label),
        meta_postgres_pod=sql_literal(postgres_pod),
        meta_kube_context=sql_literal(kube_context),
    )


def build_sql_template(*, recent_limit: int = 25) -> str:
    """SQL with placeholders for kubectl-only run.sh (no Python at runtime)."""
    return _report_body(
        start_expr=START_EXPR,
        end_expr=END_EXPR,
        tz=TZ_LITERAL,
        bucket_literal=BUCKET_LITERAL,
        recent_limit=str(recent_limit),
        meta_namespace=META_NAMESPACE,
        meta_since=META_SINCE,
        meta_until=META_UNTIL,
        meta_postgres_pod=META_POSTGRES_POD,
        meta_kube_context=META_KUBE_CONTEXT,
    )
