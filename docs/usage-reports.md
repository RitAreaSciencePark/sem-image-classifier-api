# Usage reports

Visual HTML reports of API usage from the KrakenD usage-tracking plugin. Data lives in PostgreSQL (`api_usage` on `postgresql-0`); reports are generated from your laptop via `kubectl exec`.

## Quick start

After deploy (dev or prod):

```bash
cd services/<id>/generated/prod/usage-report   # or dev/
./run.sh --namespace <namespace> --since 30d --output-dir ./reports
# → reports/api-usage_<ns>_since-30d_until-now_bucket-hour_tz-UTC_<utc>.html
```

Open the HTML file in a browser. No port-forward or repo checkout required in production — extract `usage-report/` from the prod bundle.

Dev shortcut:

```bash
make usage-report SERVICE=sem-classifier
# → /tmp/api-usage_....html
```

## Pre-production (kdevel)

```bash
./run.sh \
  --context reusable-ml-services@kdevel \
  --namespace reusable-ml-services \
  --since 30d \
  --output-dir ./reports
```

## Output files

Each run writes a **unique HTML file** whose name encodes the query parameters:

```
api-usage_<namespace>_since-<since>_until-<until>_bucket-<bucket>_tz-<tz>_<utc>.html
```

Re-running with identical parameters still produces a new file (UTC timestamp differs).

Use `--format json` for raw JSON (debugging) or `--format summary` for plain text.

## `report_meta` (embedded in query)

The SQL payload includes metadata used when rendering HTML:

| Field | Description |
|-------|-------------|
| `report_version` | Schema version |
| `generator` | `usage-report/run.sh` |
| `namespace` | Kubernetes namespace |
| `since` / `until` | Requested time window |
| `bucket` | Resolved time bucket |
| `timezone` | PostgreSQL timezone for bucketing |
| `postgres_pod` | Exec target (default `postgresql-0`) |
| `kube_context` | kubeconfig context if set |
| `generated_at_utc` | Generation timestamp |

## Requirements

| Tool | Purpose |
|------|---------|
| `kubectl` + `pods/exec` | Query PostgreSQL inside the cluster |
| `bash` | Orchestrate exec and write local files |
| `python3` | Render HTML (bundled `usage_report.py`) |

## Verify access

```bash
export NS=<namespace>
kubectl get pod postgresql-0 -n "$NS"
kubectl exec -n "$NS" postgresql-0 -- pg_isready -U krakend -d krakend
kubectl auth can-i create pods/exec -n "$NS"
```

## Post-deploy checklist

- [ ] Stack deployed and pods Ready
- [ ] `./run.sh --since 24h` writes an HTML file
- [ ] HTML shows expected traffic after a test request

See also [scripts/reports/README.md](../scripts/reports/README.md).
