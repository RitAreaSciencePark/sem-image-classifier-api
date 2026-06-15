# Scripts

Operational and research tooling for the multi-service platform. All scripts accept `SERVICE=<id>` (or `--service`) and load defaults from `services/<id>/generated/dev/deploy.env` and `secrets.local.yaml`.

Shared helpers live in [`ml_platform/devtools/`](../ml_platform/devtools/):

| Module | Purpose |
|--------|---------|
| `service_context.py` | Resolve base URL, namespace, OIDC settings per service |
| `api_client.py` | `InferenceClient`, Authentik token, job polling |
| `cli.py` | Shared `--service` flags for scripts |
| `e2e_config.py` | Optional `services/<id>/e2e.yaml` loader |

End-to-end tests are under [`../tests/`](../tests/README.md) — they import from `ml_platform/devtools`, not from `scripts/`.

## Benchmarks (paper evaluation)

[`benchmarks/benchmark_suite.py`](benchmarks/benchmark_suite.py) — reproducible latency/throughput scenarios for research.

[`benchmarks/autoscale_validate.py`](benchmarks/autoscale_validate.py) — burst-load HPA validation (queue + replica telemetry).

[`benchmarks/run_paper_benchmarks.sh`](benchmarks/run_paper_benchmarks.sh) — full scenario matrix with BentoML scale-out.

```bash
make autoscale-validate SERVICE=sem-classifier
```

See [`benchmarks/README.md`](benchmarks/README.md).

## Load testing

[`load/stress_test.py`](load/stress_test.py) — generate authenticated traffic for stress and usage-report validation.

```bash
make stress-test SERVICE=sem-classifier
# or
SERVICE=sem-classifier python scripts/load/stress_test.py --requests 50 --poll
```

## Usage reports

Production: bundled **`usage-report/run.sh`** writes **HTML** by default (included in `prod-bundle.tar.gz`).

```bash
make usage-report SERVICE=sem-classifier
cd services/sem-classifier/generated/prod/usage-report
./run.sh --namespace sem-classifier --since 30d --output-dir ./reports
```

Operator guide: [docs/usage-reports.md](../docs/usage-reports.md).
