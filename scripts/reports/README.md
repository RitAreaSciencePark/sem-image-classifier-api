# Usage reports

Read API usage from PostgreSQL `api_usage` (KrakenD usage-tracking plugin) and produce **HTML reports** for visual inspection.

## Where this runs

**You do not deploy a report pod.** PostgreSQL (`postgresql-0`) already records every authenticated request. Report tools **read** that data via `kubectl exec` from your laptop.

```
Your laptop (bash + kubectl + python3)     Kubernetes namespace
┌──────────────────────────────┐          ┌─────────────────────────┐
│  usage-report/run.sh         │ kubectl  │  postgresql-0           │
│  → .html file (open browser) │ exec ──► │  └─ api_usage table     │
└──────────────────────────────┘          └─────────────────────────┘
```

| Requirement | Why |
|-------------|-----|
| `kubectl` + `pods/exec` | Query PostgreSQL inside the pod |
| `bash` | Orchestrate exec and write local files |
| `python3` | Render HTML (bundled `usage_report.py`) |
| Prod bundle extracted | `usage-report/` ships with `make prod-pack` |

---

## Production (canonical)

```bash
tar -xzf prod-bundle.tar.gz
cd usage-report
./run.sh --namespace <prod-namespace> --since 30d --output-dir ./reports
# Open reports/api-usage_....html in a browser
```

Pre-production:

```bash
./run.sh \
  --context reusable-ml-services@kdevel \
  --namespace reusable-ml-services \
  --since 30d \
  --output-dir ./reports
```

## Dev shortcut

```bash
make usage-report SERVICE=sem-classifier
```

## Other formats

```bash
./run.sh --format json --output-dir ./reports    # raw JSON (debugging)
./run.sh --format summary --output-dir ./reports # plain text
python3 usage_report.py report --input reports/....json --output custom.html
```

## Filename and metadata

```
api-usage_<namespace>_since-<since>_until-<until>_bucket-<bucket>_tz-<tz>_<utc>.html
```

JSON payloads include `report_meta` with namespace, window, bucket, and generation time.

Full operator guide: [docs/usage-reports.md](../../docs/usage-reports.md).

## Verify access

```bash
export NS=<namespace>
kubectl get pod postgresql-0 -n "$NS"
kubectl exec -n "$NS" postgresql-0 -- pg_isready -U krakend -d krakend
kubectl auth can-i create pods/exec -n "$NS"
```

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `python3 is required for HTML` | Install Python 3 or use `--format json` |
| `namespace required` | Pass `--namespace` or set in `../deploy.env` |
| `pods "postgresql-0" not found` | Wrong namespace or stack not deployed |
| Empty report | No traffic in window |

Schema: [`db/schema.sql`](../../db/schema.sql).
