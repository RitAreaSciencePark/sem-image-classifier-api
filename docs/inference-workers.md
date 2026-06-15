# Inference workers

BentoML pods process jobs from Redis using a **background worker** with **micro-batching**.

## Submit path

- **URL jobs:** validated at submit, enqueued immediately; image is fetched in the worker.
- **Upload jobs:** image bytes are stored in Redis (base64 JSON payload).

## Final configuration

Defaults in [`ml_platform/config.yaml`](../ml_platform/config.yaml), rendered to `bentoml-config` ConfigMap:

| Variable | Default | Meaning |
|---|---|---|
| `INFERENCE_BATCH_SIZE` | **`16`** | Max jobs per ViT forward pass |
| `INFERENCE_BATCH_MAX_WAIT_MS` | `100` | Max wait to fill a batch (cap + wait, not wait-until-full) |
| `WORKER_POLL_INTERVAL_MS` | `50` | Idle poll interval |

**Batch size rationale:** Controlled sweep on Stencil (1000 jobs, heavy profile, 2026-06-14) compared batch 4/8/16/32. **Batch 16** had the highest **worker drain rate** (62.9 jobs/min = `submits_ok / (submit_wall + drain)`) with 100% submit success and peak memory ~1030 Mi (under 2 Gi limit). Larger batches did not improve worker throughput; batch 4 showed a stale-config artifact on the first run and is excluded from the final pick.

PyTorch uses its default CPU thread count (no `TORCH_NUM_THREADS` knob).

Regenerate and deploy (preferred):

```bash
make render SERVICE=sem-classifier
make deploy SERVICE=sem-classifier
```

Hot-patch ConfigMap only (without full redeploy):

```bash
kubectl apply -f services/sem-classifier/generated/dev/bentoml-config.yaml
kubectl rollout restart deploy/bentoml -n sem-classifier
```

## Health endpoint

`GET /health` includes worker settings:

```json
"worker": { "batch_size": 16, "batch_max_wait_ms": 100 }
```

## Throughput model

Worker-focused drain rate (benchmark metric):

```
worker_drain_jobs_per_min ≈ submits_ok / (submit_wall_s + drain_s) × 60
```

Approximate parallel capacity:

```
throughput ≈ replicas × batch_size / batch_latency
```

Horizontal scaling uses **CPU HPA** (see [autoscaling.md](autoscaling.md)); micro-batching and replicas multiply throughput.

## Validation

```bash
export AUTH_CLIENT_SECRET="$(kubectl get secret app-secrets -n sem-classifier \
  -o jsonpath='{.data.oidc-client-secret}' | base64 -d)"

# Light (50 jobs) — HPA scale-out check
make autoscale-validate SERVICE=sem-classifier

# Heavy (1000 jobs)
make autoscale-validate-heavy SERVICE=sem-classifier PHASE=my-run \
  OUTPUT_DIR=services/sem-classifier/generated/dev/benchmarks/my-run

# Batch comparison sweep
python3 scripts/benchmarks/autoscale_validate.py --service sem-classifier \
  --profile heavy --phase batch-16 --skip-reset \
  --output-dir services/sem-classifier/generated/dev/benchmarks/final-sweep/batch-16
```

See [`scripts/benchmarks/README.md`](../scripts/benchmarks/README.md) for metric definitions.
