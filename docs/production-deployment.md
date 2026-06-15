# Deploying to Production

## Generator-first workflow

Production does **not** use `k8s/app.sh` or `k8s/infra.sh`. Each service ships a pre-rendered bundle from the same templates as dev.

### On the build machine

```bash
cp services/sem-classifier/prod.overlay.yaml.example services/sem-classifier/prod.overlay.yaml
# Required fields:
#   kubernetes.namespace  — platform-assigned prod namespace
#   auth.jwk_url, auth.issuer, ingress.host
#   optional: image digest pin

make render-prod SERVICE=sem-classifier
make verify-prod SERVICE=sem-classifier    # MUST pass (no REPLACE_WITH_* left)
make prod-pack SERVICE=sem-classifier      # → generated/prod-bundle.tar.gz
```

Repeat per service or use `make render-prod-all && make prod-pack-all`.

Secrets and filled overlay live **outside the repository**.

### On the prod cluster (manual, ordered)

Extract the bundle or `cd` into `services/<id>/generated/prod/`. Follow `preflight-checklist.md` and `apply-order.txt`.

Example for `sem-classifier` (paths relative to `generated/prod/`):

```bash
export NS=sem-classifier
cd services/sem-classifier/generated/prod

# 1. Secrets and BentoML config (files from secure store, not git)
kubectl apply -n "$NS" -f /secure/sem-classifier/secrets.local.yaml
kubectl apply -n "$NS" -f bentoml-config.yaml

# 2. KrakenD ConfigMaps — from repository root:
cd /path/to/sem-classifier-api
k8s/render-gateway-configmaps.sh \
  --namespace "$NS" \
  --gateway-settings "services/sem-classifier/generated/prod/gateway-settings.json" \
  --service-name sem-classifier

# 3. Data layer
cd services/sem-classifier/generated/prod
kubectl apply -n "$NS" -f k8s/01-redis.yaml
kubectl rollout status statefulset/redis -n "$NS" --timeout=120s
kubectl apply -n "$NS" -f k8s/03-postgresql.yaml
kubectl rollout status statefulset/postgresql -n "$NS" --timeout=120s

# 4. Gateway + inference
kubectl apply -n "$NS" -f k8s/04-krakend.yaml
kubectl rollout status deployment/krakend -n "$NS" --timeout=300s
kubectl apply -n "$NS" -f k8s/02-bentoml.yaml
kubectl rollout status deployment/bentoml -n "$NS" --timeout=600s

# 5. Autoscaling + ingress
kubectl apply -n "$NS" -f k8s/07-hpa.yaml
kubectl apply -n "$NS" -f k8s/05-ingress.yaml
```

Platform operators may apply via GitOps instead of shell — preserve **apply order** (ConfigMaps before KrakenD, Redis before BentoML worker).

### Usage reports (operators)

Usage data is stored in the **already-deployed** PostgreSQL pod (`postgresql-0`). Reports read it via `kubectl exec` from your laptop — you do **not** deploy a report pod.

The prod bundle includes `usage-report/run.sh` (writes **HTML** by default):

```bash
cd usage-report
./run.sh --namespace "$NS" --since 30d --output-dir ./reports
# Open reports/api-usage_....html in a browser
```

Pre-production example:

```bash
./run.sh \
  --context reusable-ml-services@kdevel \
  --namespace reusable-ml-services \
  --since 30d \
  --output-dir ./reports
```

Each run writes a uniquely named HTML file. Use `--format json` for raw data. Requires `python3`, `bash`, and `pods/exec` on `postgresql-0`. Full guide: [usage-reports.md](usage-reports.md).

See also [multi-service-architecture.md](multi-service-architecture.md) and [deployment-verification.md](deployment-verification.md).
