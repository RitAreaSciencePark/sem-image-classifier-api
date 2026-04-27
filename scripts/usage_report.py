#!/usr/bin/env python3
"""Generate terminal, JSON, or static HTML reports from api_usage."""

from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RELATIVE_RE = re.compile(r"^(\d+)(s|m|h|d|w)$")


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError(f"value must be > 0: {value}")
    return parsed


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def time_expr(value: str | None, *, default_now: bool) -> str:
    if not value:
        return "NOW()" if default_now else "NULL::timestamptz"
    normalized = value.strip().lower()
    if normalized == "now":
        return "NOW()"
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


def build_sql(
    *,
    since: str | None,
    until: str | None,
    timezone_name: str,
    bucket: str,
    recent_limit: int,
) -> str:
    start_expr = time_expr(since, default_now=False)
    end_expr = time_expr(until, default_now=True)
    tz = sql_literal(timezone_name)
    bucket_literal = sql_literal(bucket)

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


def run_query(namespace: str, sql: str) -> dict[str, Any]:
    cmd = [
        "kubectl",
        "exec",
        "-i",
        "-n",
        namespace,
        "postgresql-0",
        "--",
        "bash",
        "-lc",
        'PGPASSWORD="$POSTGRES_PASSWORD" psql -U krakend -d krakend -v ON_ERROR_STOP=1 -t -A',
    ]
    proc = subprocess.run(cmd, input=sql, text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "kubectl/psql failed").strip()
        raise RuntimeError(msg)
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    for candidate in ([lines[-1]] if lines else []) + [
        "".join(lines),
        proc.stdout.strip(),
    ]:
        try:
            data = json.loads(candidate)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    raise RuntimeError("failed to parse JSON from SQL output")


def pct(part: int, total: int) -> str:
    return "0.0%" if total <= 0 else f"{(part / total) * 100:.1f}%"


def bar(value: int, max_value: int, width: int = 28) -> str:
    if max_value <= 0:
        return "".ljust(width)
    filled = max(1 if value > 0 else 0, round((value / max_value) * width))
    return ("#" * filled).ljust(width)


def render_rows(title: str, rows: list[dict[str, Any]], limit: int = 10) -> list[str]:
    out = [title]
    if not rows:
        return out + ["  (none)"]
    max_value = max(int(row.get("n", 0)) for row in rows) or 1
    for row in rows[:limit]:
        name = str(row.get("name") or row.get("bucket") or "<unknown>")
        n = int(row.get("n", 0))
        out.append(f"  {name[:34]:34} {n:6d}  {bar(n, max_value)}")
    return out


def render_summary(report: dict[str, Any]) -> str:
    total = int(report.get("total") or 0)
    errors = int(report.get("error_count") or 0)
    unique_users = int(report.get("unique_users") or 0)
    coverage_start = report.get("coverage_start") or "no data"
    coverage_end = report.get("coverage_end") or "no data"
    bucket = report.get("bucket", "hour")

    lines = [
        "SEM API Usage Summary",
        f"Generated UTC: {report.get('generated_at_utc', '')}",
        f"Timezone: {report.get('timezone', 'UTC')} | Bucket: {bucket}",
        f"Coverage: {coverage_start} -> {coverage_end}",
        "",
        f"Total requests: {total}",
        f"Unique users  : {unique_users}",
        f"Errors        : {errors} ({pct(errors, total)})",
        "",
    ]
    lines.extend(render_rows("Endpoint mix:", report.get("endpoint_counts") or []))
    lines.append("")
    lines.extend(render_rows("Status health:", report.get("status_counts") or []))
    lines.append("")
    lines.extend(render_rows("Top users:", report.get("user_counts") or []))
    lines.append("")
    lines.extend(
        render_rows(
            "Recent activity by bucket:", report.get("time_buckets") or [], limit=12
        )
    )
    lines.append("")
    lines.append("Recent requests:")
    recent = report.get("recent") or []
    if not recent:
        lines.append("  (none)")
    else:
        for row in recent[:8]:
            lines.append(
                f"  {row.get('timestamp', '')}  {str(row.get('username', ''))[:18]:18} "
                f"{str(row.get('endpoint_type', ''))[:12]:12} {row.get('status_code')} {row.get('url_path')}"
            )
    return "\n".join(lines)


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def timeline_svg(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return '<div class="empty">No traffic in this window.</div>'
    width, height = 900, 230
    pad_l, pad_r, pad_t, pad_b = 48, 20, 22, 44
    chart_w = width - pad_l - pad_r
    chart_h = height - pad_t - pad_b
    max_n = max(int(row.get("n", 0)) for row in rows) or 1
    bar_w = max(2, chart_w / max(len(rows), 1) * 0.72)
    parts = [
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Traffic timeline">'
    ]
    parts.append(
        f'<line x1="{pad_l}" y1="{height - pad_b}" x2="{width - pad_r}" y2="{height - pad_b}" class="axis"/>'
    )
    for index, row in enumerate(rows):
        n = int(row.get("n", 0))
        errors = int(row.get("errors", 0))
        x = pad_l + (index + 0.14) * (chart_w / max(len(rows), 1))
        h = (n / max_n) * chart_h
        y = height - pad_b - h
        cls = "bar err" if errors else "bar"
        parts.append(
            f'<rect class="{cls}" x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{h:.1f}"><title>{esc(row.get("bucket"))}: {n} requests, {errors} errors</title></rect>'
        )
    first = esc(rows[0].get("bucket", ""))
    last = esc(rows[-1].get("bucket", ""))
    parts.append(f'<text x="{pad_l}" y="{height - 14}" class="tick">{first}</text>')
    parts.append(
        f'<text x="{width - pad_r}" y="{height - 14}" text-anchor="end" class="tick">{last}</text>'
    )
    parts.append(f'<text x="{pad_l}" y="16" class="tick">max {max_n}</text>')
    parts.append("</svg>")
    return "".join(parts)


def html_bars(rows: list[dict[str, Any]], title: str, *, status: bool = False) -> str:
    if not rows:
        return f'<section class="panel"><h2>{esc(title)}</h2><p class="empty">No data.</p></section>'
    max_n = max(int(row.get("n", 0)) for row in rows) or 1
    items = []
    for row in rows[:12]:
        name = str(row.get("name", "<unknown>"))
        n = int(row.get("n", 0))
        width = (n / max_n) * 100
        cls = "warn" if status and (not name.startswith("2")) else ""
        items.append(
            f'<div class="bar-row"><span>{esc(name)}</span><strong>{n}</strong>'
            f'<div class="bar-track"><i class="{cls}" style="width:{width:.1f}%"></i></div></div>'
        )
    return f'<section class="panel"><h2>{esc(title)}</h2>{"".join(items)}</section>'


def heatmap(rows: list[dict[str, Any]]) -> str:
    values = {(int(r["dow"]), int(r["hour"])): int(r["n"]) for r in rows}
    max_n = max(values.values()) if values else 0
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    cells = ['<div class="heat corner"></div>']
    for hour in range(24):
        cells.append(f'<div class="heat label">{hour:02d}</div>')
    for dow, day in enumerate(days, start=1):
        cells.append(f'<div class="heat label day">{day}</div>')
        for hour in range(24):
            n = values.get((dow, hour), 0)
            alpha = 0 if max_n == 0 else 0.12 + 0.78 * (n / max_n)
            cells.append(
                f'<div class="heat cell" style="--a:{alpha:.2f}"><span>{n}</span></div>'
            )
    return (
        '<section class="panel wide"><h2>Hourly Activity Heatmap</h2><div class="heatmap">'
        + "".join(cells)
        + "</div></section>"
    )


def recent_table(rows: list[dict[str, Any]]) -> str:
    body = []
    for row in rows:
        body.append(
            "<tr>"
            f"<td>{esc(row.get('timestamp'))}</td>"
            f"<td>{esc(row.get('username'))}</td>"
            f"<td>{esc(row.get('endpoint_type'))}</td>"
            f"<td>{esc(row.get('status_code'))}</td>"
            f"<td>{esc(row.get('url_path'))}</td>"
            "</tr>"
        )
    if not body:
        body.append('<tr><td colspan="5">No recent requests.</td></tr>')
    return (
        '<section class="panel wide"><h2>Recent Requests</h2><table><thead><tr><th>Time</th><th>User</th><th>Endpoint</th><th>Status</th><th>Path</th></tr></thead><tbody>'
        + "".join(body)
        + "</tbody></table></section>"
    )


def render_html(report: dict[str, Any]) -> str:
    total = int(report.get("total") or 0)
    errors = int(report.get("error_count") or 0)
    unique_users = int(report.get("unique_users") or 0)
    coverage_start = report.get("coverage_start") or "no data"
    coverage_end = report.get("coverage_end") or "no data"
    generated = report.get("generated_at_utc", "")
    css = """
:root{--ink:#17211f;--muted:#63706c;--paper:#f5f1e8;--panel:#fffaf0;--line:#ded6c7;--teal:#197b72;--amber:#b86b11;--red:#b42318;--green:#2b7a3d}*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at top left,#e7f0ea,transparent 28rem),var(--paper);color:var(--ink);font-family:Aptos,"IBM Plex Sans","Segoe UI",sans-serif}.wrap{max-width:1180px;margin:0 auto;padding:36px 24px 56px}.hero{border:1px solid var(--line);background:linear-gradient(135deg,#fffaf0,#edf7f4);border-radius:28px;padding:28px 32px;box-shadow:0 18px 50px #3c2d1712}.eyebrow{letter-spacing:.14em;text-transform:uppercase;color:var(--teal);font-weight:800;font-size:12px}h1{font-size:44px;line-height:1;margin:10px 0 12px}p{color:var(--muted)}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:18px 0}.card,.panel{border:1px solid var(--line);background:var(--panel);border-radius:22px;padding:20px;box-shadow:0 12px 34px #3c2d1710}.card small{display:block;color:var(--muted);font-weight:700;text-transform:uppercase;letter-spacing:.08em}.card strong{display:block;font-size:32px;margin-top:8px}.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:22px 0}.panels{display:grid;grid-template-columns:repeat(2,1fr);gap:16px}.wide{grid-column:1/-1}h2{font-size:18px;margin:0 0 16px}.bar-row{display:grid;grid-template-columns:minmax(0,1fr) 64px;gap:10px;align-items:center;margin:12px 0}.bar-row span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.bar-row strong{text-align:right}.bar-track{grid-column:1/-1;height:10px;border-radius:999px;background:#eadfce;overflow:hidden}.bar-track i{display:block;height:100%;background:var(--teal);border-radius:999px}.bar-track i.warn{background:var(--amber)}svg{width:100%;height:auto}.axis{stroke:#cfc5b5}.bar{fill:var(--teal)}.bar.err{fill:var(--red)}.tick{fill:var(--muted);font-size:12px}.heatmap{display:grid;grid-template-columns:52px repeat(24,1fr);gap:3px}.heat{height:24px;display:flex;align-items:center;justify-content:center;font-size:10px;color:var(--muted)}.heat.cell{background:rgba(25,123,114,var(--a));border-radius:5px;color:transparent}.heat.cell:hover{color:var(--ink);outline:1px solid var(--teal)}.heat.day{justify-content:flex-start;font-weight:700}table{width:100%;border-collapse:collapse;font-size:14px}th,td{border-bottom:1px solid var(--line);padding:10px 8px;text-align:left}th{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.08em}.empty{color:var(--muted);font-style:italic}.notes{font-size:14px;color:var(--muted)}@media(max-width:850px){.cards,.grid,.panels{grid-template-columns:1fr}h1{font-size:34px}.heatmap{overflow-x:auto;display:flex}.heat{min-width:24px}}
""".strip()
    return f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>SEM API Usage Report</title><style>{css}</style></head>
<body><main class="wrap">
<section class="hero">
  <div class="eyebrow">Static Usage Report</div>
  <h1>SEM API Usage</h1>
  <p>Generated {esc(generated)} UTC. Coverage in {esc(report.get("timezone", "UTC"))}: {esc(coverage_start)} to {esc(coverage_end)}.</p>
</section>
<section class="cards">
  <div class="card"><small>Total Requests</small><strong>{total}</strong></div>
  <div class="card"><small>Unique Users</small><strong>{unique_users}</strong></div>
  <div class="card"><small>Errors</small><strong>{errors}</strong></div>
  <div class="card"><small>Error Rate</small><strong>{pct(errors, total)}</strong></div>
</section>
<section class="panel wide"><h2>Traffic Timeline</h2>{timeline_svg(report.get("time_buckets") or [])}</section>
<section class="panels">
  {html_bars(report.get("endpoint_counts") or [], "Endpoint Mix")}
  {html_bars(report.get("status_counts") or [], "Status Health", status=True)}
  {html_bars(report.get("user_counts") or [], "Top Users")}
  {html_bars(report.get("path_counts") or [], "Top Paths")}
  {heatmap(report.get("hour_heatmap") or [])}
  {recent_table(report.get("recent") or [])}
  <section class="panel wide notes"><h2>Data Quality Notes</h2><p>Rows come from KrakenD plugin observations of tracked authenticated routes. Usage writes are asynchronous, so a successful API response can briefly precede its database row. Redis job state and PostgreSQL usage evidence are intentionally separate stores.</p></section>
</section>
</main></body></html>
"""


def parse_args(argv: list[str]) -> argparse.Namespace:
    modes = {"summary", "report", "json"}
    mode = argv[0] if argv and argv[0] in modes else "summary"
    rest = argv[1:] if argv and argv[0] in modes else argv

    parser = argparse.ArgumentParser(
        description="Generate API usage reports from in-cluster PostgreSQL."
    )
    parser.add_argument("--namespace", default="sem-image-classifier")
    parser.add_argument(
        "--since",
        default=None,
        help="Start time, ISO timestamp, or relative value like 24h/7d. Defaults to 24h for summary/json and full history for report.",
    )
    parser.add_argument(
        "--until",
        default=None,
        help="End time, ISO timestamp, or 'now'. Defaults to now.",
    )
    parser.add_argument("--timezone", default="UTC")
    parser.add_argument("--output", default="")
    parser.add_argument(
        "--bucket", choices=("auto", "minute", "hour", "day"), default="auto"
    )
    parser.add_argument("--recent-limit", type=positive_int, default=25)
    parser.add_argument(
        "--window-mins",
        type=positive_int,
        default=None,
        help="Backward-compatible shortcut for --since <N>m.",
    )
    args = parser.parse_args(rest)
    args.mode = mode
    if args.window_mins is not None:
        args.since = f"{args.window_mins}m"
    if args.since is None and args.mode in {"summary", "json"}:
        args.since = "24h"
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    bucket = choose_bucket(args.mode, args.since, args.bucket)
    try:
        report = run_query(
            args.namespace,
            build_sql(
                since=args.since,
                until=args.until,
                timezone_name=args.timezone,
                bucket=bucket,
                recent_limit=args.recent_limit,
            ),
        )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.mode == "json":
        rendered = json.dumps(report, indent=2, sort_keys=True)
        default_output = ""
    elif args.mode == "report":
        rendered = render_html(report)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        default_output = f"/tmp/sem-usage-report-{stamp}.html"
    else:
        rendered = render_summary(report)
        default_output = ""

    output = args.output or default_output
    if output:
        Path(output).write_text(rendered + "\n", encoding="utf-8")
        print(f"Wrote report to {output}", file=sys.stderr)
    if args.mode != "report" or not output:
        print(rendered)
    elif output:
        print(f"HTML report: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
