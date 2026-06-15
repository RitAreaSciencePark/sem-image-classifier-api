#!/usr/bin/env python3
"""API usage reports from PostgreSQL api_usage (KrakenD plugin).

Production: use bundled ``run.sh`` (bash + kubectl + python3) — writes HTML by default.
This script renders HTML/summary from saved JSON or live-queries from dev.

See scripts/reports/README.md and generated ``usage-report/README.md``.
"""

from __future__ import annotations

import argparse
import html
import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

POSTGRES_POD = "postgresql-0"


def _load_module(name: str):
    try:
        return importlib.import_module(f"ml_platform.usage_report.{name}")
    except ImportError:
        path = Path(__file__).resolve().parent / f"{name}.py"
        if not path.is_file():
            raise
        spec = importlib.util.spec_from_file_location(f"usage_report_{name}", path)
        if spec is None or spec.loader is None:
            raise ImportError(name)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod


_sql = _load_module("sql")
_names = _load_module("names")

build_sql = _sql.build_sql
choose_bucket = _sql.choose_bucket
time_expr = _sql.time_expr
report_filename = _names.report_filename


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError(f"value must be > 0: {value}")
    return parsed


def namespace_from_report(report: dict[str, Any], fallback: str = "") -> str:
    meta = report.get("report_meta") or {}
    return str(meta.get("namespace") or fallback or "unknown")


def run_query(
    namespace: str,
    sql: str,
    *,
    postgres_pod: str = POSTGRES_POD,
    kubectl: str = "kubectl",
    kube_context: str = "",
) -> dict[str, Any]:
    cmd = [kubectl]
    if kube_context:
        cmd.extend(["--context", kube_context])
    cmd.extend(
        [
            "exec",
            "-i",
            "-n",
            namespace,
            postgres_pod,
            "--",
            "bash",
            "-lc",
            'PGPASSWORD="$POSTGRES_PASSWORD" psql -U krakend -d krakend -v ON_ERROR_STOP=1 -t -A',
        ]
    )
    proc = subprocess.run(cmd, input=sql, text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "kubectl/psql failed").strip())
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    for candidate in ([lines[-1]] if lines else []) + ["".join(lines), proc.stdout.strip()]:
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


def render_summary(report: dict[str, Any], *, namespace: str) -> str:
    total = int(report.get("total") or 0)
    errors = int(report.get("error_count") or 0)
    unique_users = int(report.get("unique_users") or 0)
    meta = report.get("report_meta") or {}
    lines = [
        "API Usage Summary",
        f"Namespace: {namespace}",
        f"Generated UTC: {report.get('generated_at_utc', meta.get('generated_at_utc', ''))}",
        f"Since: {meta.get('since', 'n/a')} | Until: {meta.get('until', 'now')}",
        f"Timezone: {report.get('timezone', 'UTC')} | Bucket: {report.get('bucket', 'hour')}",
        f"Coverage: {report.get('coverage_start') or 'no data'} -> {report.get('coverage_end') or 'no data'}",
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
        render_rows("Activity by bucket:", report.get("time_buckets") or [], limit=12)
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
    chart_w, chart_h = width - pad_l - pad_r, height - pad_t - pad_b
    max_n = max(int(row.get("n", 0)) for row in rows) or 1
    bar_w = max(2, chart_w / max(len(rows), 1) * 0.72)
    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Traffic timeline">']
    parts.append(
        f'<line x1="{pad_l}" y1="{height - pad_b}" x2="{width - pad_r}" y2="{height - pad_b}" class="axis"/>'
    )
    for index, row in enumerate(rows):
        n, errors = int(row.get("n", 0)), int(row.get("errors", 0))
        x = pad_l + (index + 0.14) * (chart_w / max(len(rows), 1))
        h = (n / max_n) * chart_h
        cls = "bar err" if errors else "bar"
        parts.append(
            f'<rect class="{cls}" x="{x:.1f}" y="{height - pad_b - h:.1f}" width="{bar_w:.1f}" height="{h:.1f}">'
            f"<title>{esc(row.get('bucket'))}: {n} requests, {errors} errors</title></rect>"
        )
    parts.append(f'<text x="{pad_l}" y="{height - 14}" class="tick">{esc(rows[0].get("bucket", ""))}</text>')
    parts.append(
        f'<text x="{width - pad_r}" y="{height - 14}" text-anchor="end" class="tick">{esc(rows[-1].get("bucket", ""))}</text>'
    )
    parts.append("</svg>")
    return "".join(parts)


def html_bars(rows: list[dict[str, Any]], title: str, *, status: bool = False) -> str:
    if not rows:
        return f'<section class="panel"><h2>{esc(title)}</h2><p class="empty">No data.</p></section>'
    max_n = max(int(row.get("n", 0)) for row in rows) or 1
    items = []
    for row in rows[:12]:
        name, n = str(row.get("name", "<unknown>")), int(row.get("n", 0))
        cls = "warn" if status and not name.startswith("2") else ""
        items.append(
            f'<div class="bar-row"><span>{esc(name)}</span><strong>{n}</strong>'
            f'<div class="bar-track"><i class="{cls}" style="width:{(n / max_n) * 100:.1f}%"></i></div></div>'
        )
    return f'<section class="panel"><h2>{esc(title)}</h2>{"".join(items)}</section>'


def heatmap(rows: list[dict[str, Any]]) -> str:
    values = {(int(r["dow"]), int(r["hour"])): int(r["n"]) for r in rows}
    max_n = max(values.values()) if values else 0
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    cells = ['<div class="heat corner"></div>'] + [
        f'<div class="heat label">{h:02d}</div>' for h in range(24)
    ]
    for dow, day in enumerate(days, start=1):
        cells.append(f'<div class="heat label day">{day}</div>')
        for hour in range(24):
            n = values.get((dow, hour), 0)
            alpha = 0 if max_n == 0 else 0.12 + 0.78 * (n / max_n)
            cells.append(f'<div class="heat cell" style="--a:{alpha:.2f}"><span>{n}</span></div>')
    return (
        '<section class="panel wide"><h2>Hourly Activity Heatmap</h2><div class="heatmap">'
        + "".join(cells)
        + "</div></section>"
    )


def recent_table(rows: list[dict[str, Any]]) -> str:
    body = [
        "<tr>"
        f"<td>{esc(r.get('timestamp'))}</td><td>{esc(r.get('username'))}</td>"
        f"<td>{esc(r.get('endpoint_type'))}</td><td>{esc(r.get('status_code'))}</td>"
        f"<td>{esc(r.get('url_path'))}</td></tr>"
        for r in rows
    ] or ['<tr><td colspan="5">No recent requests.</td></tr>']
    return (
        '<section class="panel wide"><h2>Recent Requests</h2><table><thead><tr>'
        "<th>Time</th><th>User</th><th>Endpoint</th><th>Status</th><th>Path</th></tr></thead><tbody>"
        + "".join(body)
        + "</tbody></table></section>"
    )


def render_html(report: dict[str, Any], *, namespace: str) -> str:
    total = int(report.get("total") or 0)
    errors = int(report.get("error_count") or 0)
    meta = report.get("report_meta") or {}
    css = ":root{--ink:#17211f;--muted:#63706c;--paper:#f5f1e8;--panel:#fffaf0;--line:#ded6c7;--teal:#197b72;--amber:#b86b11;--red:#b42318}*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font-family:system-ui,sans-serif}.wrap{max-width:1100px;margin:0 auto;padding:32px 20px}h1{margin:0 0 8px}.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:20px 0}.card,.panel{border:1px solid var(--line);background:var(--panel);border-radius:16px;padding:16px}.card strong{font-size:28px}.panels{display:grid;grid-template-columns:1fr 1fr;gap:14px}.wide{grid-column:1/-1}svg{width:100%}.axis{stroke:#cfc5b5}.bar{fill:var(--teal)}.bar.err{fill:var(--red)}.tick{fill:var(--muted);font-size:12px}.bar-row{display:grid;grid-template-columns:1fr 48px;gap:8px;margin:8px 0}.bar-track{grid-column:1/-1;height:8px;background:#eadfce;border-radius:999px;overflow:hidden}.bar-track i{display:block;height:100%;background:var(--teal)}.bar-track i.warn{background:var(--amber)}table{width:100%;border-collapse:collapse;font-size:14px}th,td{border-bottom:1px solid var(--line);padding:8px;text-align:left}.heatmap{display:grid;grid-template-columns:52px repeat(24,1fr);gap:2px}.heat{height:22px;font-size:10px;display:flex;align-items:center;justify-content:center}.heat.cell{background:rgba(25,123,114,var(--a));border-radius:4px;color:transparent}"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>API Usage — {esc(namespace)}</title><style>{css}</style></head>
<body><main class="wrap">
<h1>API Usage Report</h1>
<p>Namespace: {esc(namespace)} · Generated {esc(report.get('generated_at_utc', ''))} UTC</p>
<p>Window: {esc(meta.get('since', ''))} → {esc(meta.get('until', 'now'))} · Bucket: {esc(report.get('bucket', ''))}</p>
<p>Coverage: {esc(report.get('coverage_start'))} → {esc(report.get('coverage_end'))} ({esc(report.get('timezone', 'UTC'))})</p>
<section class="cards">
  <div class="card"><small>Requests</small><strong>{total}</strong></div>
  <div class="card"><small>Users</small><strong>{int(report.get('unique_users') or 0)}</strong></div>
  <div class="card"><small>Errors</small><strong>{errors}</strong></div>
  <div class="card"><small>Error rate</small><strong>{pct(errors, total)}</strong></div>
</section>
<section class="panel wide"><h2>Traffic timeline</h2>{timeline_svg(report.get('time_buckets') or [])}</section>
<section class="panels">
  {html_bars(report.get('endpoint_counts') or [], 'Endpoints')}
  {html_bars(report.get('status_counts') or [], 'Status codes', status=True)}
  {html_bars(report.get('user_counts') or [], 'Top users')}
  {html_bars(report.get('path_counts') or [], 'Top paths')}
  {heatmap(report.get('hour_heatmap') or [])}
  {recent_table(report.get('recent') or [])}
</section>
</main></body></html>"""


def parse_args(argv: list[str]) -> argparse.Namespace:
    modes = {"summary", "report", "json"}
    mode = argv[0] if argv and argv[0] in modes else "summary"
    rest = argv[1:] if argv and argv[0] in modes else argv

    parser = argparse.ArgumentParser(description="API usage report from in-cluster PostgreSQL.")
    parser.add_argument("--input", default="", help="Render from saved JSON (no kubectl)")
    parser.add_argument("--namespace", default="", help="Kubernetes namespace (live mode)")
    parser.add_argument("--since", default=None, help="Start: 24h, 7d, or ISO timestamp")
    parser.add_argument("--until", default=None, help="End (default: now)")
    parser.add_argument("--timezone", default="UTC")
    parser.add_argument("--output", default="", help="Write report to file")
    parser.add_argument("--bucket", choices=("auto", "minute", "hour", "day"), default="auto")
    parser.add_argument("--recent-limit", type=positive_int, default=25)
    parser.add_argument("--postgres-pod", default=POSTGRES_POD)
    parser.add_argument("--kubectl", default="kubectl", help="kubectl binary path")
    parser.add_argument("--context", default="", help="kubeconfig context")
    args = parser.parse_args(rest)
    args.mode = mode
    if not args.input and not args.namespace:
        parser.error("--namespace is required for live queries (or use --input)")
    if args.since is None and args.mode in {"summary", "json"} and not args.input:
        args.since = "24h"
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)

    if args.input:
        report = json.loads(Path(args.input).read_text(encoding="utf-8"))
        ns = args.namespace or namespace_from_report(report)
    else:
        bucket = choose_bucket(args.mode, args.since, args.bucket)
        since_label = args.since or "all"
        until_label = args.until or "now"
        sql = build_sql(
            since=args.since,
            until=args.until,
            timezone_name=args.timezone,
            bucket=bucket,
            recent_limit=args.recent_limit,
            namespace=args.namespace,
            since_label=since_label,
            until_label=until_label,
            postgres_pod=args.postgres_pod,
            kube_context=args.context,
        )
        try:
            report = run_query(
                args.namespace,
                sql,
                postgres_pod=args.postgres_pod,
                kubectl=args.kubectl,
                kube_context=args.context,
            )
        except Exception as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        ns = args.namespace

    if args.mode == "json":
        rendered = json.dumps(report, indent=2, sort_keys=True)
        default_output = ""
    elif args.mode == "report":
        rendered = render_html(report, namespace=ns)
        meta = report.get("report_meta") or {}
        default_output = ""
        if not args.output:
            default_output = str(
                Path("/tmp")
                / report_filename(
                    namespace=ns,
                    since=str(meta.get("since", args.since or "all")),
                    until=str(meta.get("until", "now")),
                    bucket=str(meta.get("bucket", report.get("bucket", "hour"))),
                    timezone_name=str(meta.get("timezone", args.timezone)),
                    fmt="html",
                )
            )
    else:
        rendered = render_summary(report, namespace=ns)
        default_output = ""

    output = args.output or default_output
    if output:
        Path(output).write_text(rendered + "\n", encoding="utf-8")
        print(f"Wrote {output}", file=sys.stderr)
    print(rendered if args.mode != "report" or not output else f"HTML report: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
