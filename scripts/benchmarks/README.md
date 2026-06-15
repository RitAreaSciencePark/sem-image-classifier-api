# Paper benchmarks

Reproducible evaluation suite for latency, throughput, and scale-out experiments (eScience 2026).

## Prerequisites

- Dev cluster with service deployed and port-forward active (`make access SERVICE=<id>`)
- `AUTH_CLIENT_SECRET` in env or `services/<id>/secrets.local.yaml`
- `kubectl` access to scale BentoML replicas

## Single scenario

```bash
SERVICE=sem-classifier python scripts/benchmarks/benchmark_suite.py \
  --scenario baseline \
  --trials 3 \
  --output text
```

Scenarios: `baseline`, `load-5`, `load-10`, `scaleout-3`.

## Full matrix

```bash
SERVICE=sem-classifier ./scripts/benchmarks/run_paper_benchmarks.sh
```

Runs all scenarios, scales BentoML to 3 replicas for `scaleout-3`, then restores 1 replica.

## HPA autoscale validation

Proves CPU HPA scales BentoML under burst load (no manual `kubectl scale`):

```bash
export AUTH_CLIENT_SECRET="$(kubectl get secret app-secrets -n sem-classifier \
  -o jsonpath='{.data.oidc-client-secret}' | base64 -d)"

make autoscale-validate SERVICE=sem-classifier
```

Sweep profile (500 jobs, pass on ≥95% submit success — no HPA scale-out required):

```bash
python3 scripts/benchmarks/autoscale_validate.py --service sem-classifier \
  --profile sweep --phase my-run --output-dir services/sem-classifier/generated/dev/benchmarks/my-run
```

### Metrics (`autoscale_validate.py`)

| Field | Meaning |
|---|---|
| `throughput_jobs_per_min` | End-to-end: `submits_ok / total_wall_s` |
| `worker_drain_jobs_per_min` | Worker-focused: `submits_ok / (submit_wall_s + drain_s)` |
| `peak_bentoml_memory_mib` | Max Mi from `kubectl top` telemetry during run |

Use **worker drain** to compare batch sizes; E2E throughput includes gateway/submit contention.

### Final batch-size sweep

Controlled comparison (1000 jobs, heavy profile, one replica):

```bash
export AUTH_CLIENT_SECRET="$(kubectl get secret app-secrets -n sem-classifier \
  -o jsonpath='{.data.oidc-client-secret}' | base64 -d)"

for B in 4 8 16 32; do
  kubectl patch configmap bentoml-config -n sem-classifier \
    --type merge -p "{\"data\":{\"INFERENCE_BATCH_SIZE\":\"$B\"}}"
  kubectl rollout restart deploy/bentoml -n sem-classifier
  kubectl rollout status deploy/bentoml -n sem-classifier --timeout=180s
  python3 scripts/benchmarks/autoscale_validate.py --service sem-classifier \
    --profile heavy --phase batch-$B --skip-reset \
    --output-dir services/sem-classifier/generated/dev/benchmarks/final-sweep/batch-$B
done
```

Writes JSON telemetry to `--output-dir` if set. See [`docs/autoscaling.md`](../../docs/autoscaling.md).

## Output

| Path | Content |
|------|---------|
| `--output-dir` (if set) | Per-run JSON telemetry (`autoscale-*.json`) |
| `services/<id>/generated/dev/benchmarks/` | Typical location for sweep artifacts |

Paper benchmark orchestrator reads `PF_PORT` from `services/<id>/generated/dev/deploy.env` for health checks.
