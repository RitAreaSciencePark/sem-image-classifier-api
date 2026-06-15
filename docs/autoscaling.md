# BentoML autoscaling

The SEM classifier API scales **BentoML inference pods** horizontally using **Kubernetes CPU HPA**. Each pod runs a **micro-batching worker** (default batch **16**); more replicas mean more parallel batches.

See [inference-workers.md](inference-workers.md) for worker/batch settings.

## Final mechanism: CPU HPA

**We use tuned CPU HorizontalPodAutoscaler** — not KEDA, not in-app queue scaling. This matches pre-production cluster capabilities today and is validated on Stencil.

| Component | Setting |
|---|---|
| Scaler | `HorizontalPodAutoscaler` `bentoml-hpa` on Deployment `bentoml` |
| Signal | CPU utilization vs pod CPU **request** (target **70%**) |
| Range | min **1**, max **3** replicas |
| CPU request / limit | **1000m** / **2** |
| Memory request / limit | **512Mi** / **2Gi** |
| Scale-up | **0s** stabilization, **+2 pods / 60s** |
| Scale-down | **300s** stabilization, **−1 pod / 120s** |
| Worker batch size | **16** |

Codegen source: [`ml_platform/config.yaml`](../ml_platform/config.yaml) → [`07-hpa.yaml.j2`](../ml_platform/templates/k8s/app/07-hpa.yaml.j2).

Regenerate manifests:

```bash
make render SERVICE=sem-classifier
make render-prod SERVICE=sem-classifier   # prod bundle for platform GitOps
```

## Known limitation

With micro-batching, per-pod CPU can stay **below the HPA target** while Redis queue depth grows (leading signal). Heavy bursts may backlog without scale-out until CPU rises. **Queue-driven scaling (KEDA on `queue:pending`) is a platform-admin request**, not implemented in this repository.

Empirical examples (Stencil, 2026-06-14):

- Light burst (50 jobs, batch 16): queue pending ~7, **no scale-out**
- Heavy burst (1000 jobs, batch 8): scaled **1→2** after ~883s

## Validate on Stencil

Prerequisites: cluster access, port-forward to KrakenD (8080), Authentik client secret.

```bash
export AUTH_CLIENT_SECRET="$(kubectl get secret app-secrets -n sem-classifier \
  -o jsonpath='{.data.oidc-client-secret}' | base64 -d)"

python3 scripts/benchmarks/autoscale_validate.py --service sem-classifier --profile light
```

Heavy load (1000 jobs, 80 concurrent submitters):

```bash
python3 scripts/benchmarks/autoscale_validate.py --service sem-classifier \
  --profile heavy --phase my-run \
  --output-dir services/sem-classifier/generated/dev/benchmarks/my-run
```

Exit code **0** on `--profile light` requires **≥95% submit success** (scale-out not required at light load). `--profile heavy` passes on **≥95% submit success**. `--profile sweep` passes on throughput without requiring scale-out.

## Pre-production

Pre-prod (`reusable-ml-services@kdevel`) has **no KEDA** and the app service account **cannot manage HPAs**. The prod bundle includes the same HPA YAML for **platform operators** to apply via GitOps.

**Platform escalation (optional future):**

1. HPA RBAC for the app identity
2. KEDA install + Redis scaler on `queue:pending`
3. Max replicas policy for inference pods

## Related scripts

- `scripts/benchmarks/autoscale_validate.py` — burst load + HPA/queue telemetry
- `scripts/benchmarks/benchmark_suite.py` — latency/throughput paper benchmarks
- `scripts/load/stress_test.py` — mixed API traffic
