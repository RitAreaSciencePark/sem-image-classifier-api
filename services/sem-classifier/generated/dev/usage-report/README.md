# Usage report (operator tools)

Generate **HTML** API usage reports from the **already-deployed** PostgreSQL pod (`postgresql-0`).
No report pod is deployed — this tool uses `kubectl exec` from your laptop.

## Quick start

```bash
./run.sh --namespace <namespace> --since 30d --output-dir ./reports
# → reports/api-usage_....html — open in a browser
```

Pre-production (kdevel):

```bash
./run.sh \
  --context reusable-ml-services@kdevel \
  --namespace reusable-ml-services \
  --since 30d \
  --output-dir ./reports
```

## Output files

Each run writes a **unique HTML file** encoding query parameters in the filename:

```
api-usage_<namespace>_since-<since>_until-<until>_bucket-<bucket>_tz-<tz>_<utc>.html
```

Use `--format json` for raw JSON or `--format summary` for plain text.

## Flags

| Flag | Default | Purpose |
|------|---------|---------|
| `--namespace` | from `../deploy.env` | Kubernetes namespace |
| `--context` | (current) | kubeconfig context (pre-prod) |
| `--since` | `24h` | `24h`, `7d`, `30d`, `all`, or ISO timestamp |
| `--until` | `now` | End bound |
| `--bucket` | `auto` | `minute`, `hour`, `day`, or `auto` |
| `--timezone` | `UTC` | PostgreSQL timezone for bucketing |
| `--format` | `html` | `html`, `json`, or `summary` |
| `--output-dir` | `.` | Directory for report files |
| `--postgres-pod` | `postgresql-0` | PostgreSQL StatefulSet pod |
| `--kubectl` | `kubectl` | kubectl binary path |

## Requirements

- `kubectl` with `pods/exec` on `postgresql-0`
- `bash` on your laptop
- `python3` for HTML rendering (bundled `usage_report.py`)

## Verify access

```bash
export NS=<namespace>
kubectl get pod postgresql-0 -n "$NS"
kubectl exec -n "$NS" postgresql-0 -- pg_isready -U krakend -d krakend
kubectl auth can-i create pods/exec -n "$NS"
```
