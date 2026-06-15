# Deployment Verification

Reproducible checklists for dev (automated) and prod (preflight + manual apply). Canonical clean redeploy on Stencil: **12/12 E2E** (6 tests × 2 services).

## Dev — canonical clean redeploy (all services)

Authentik namespace **`authentik-reusable-ml-services` is never deleted**.

```bash
make check-prereqs
make render-all ENV=dev
make fresh-all          # per service: delete NS + deploy --rebuild + infra-configure
make test-all           # port-forwards + E2E; reads secrets.local.yaml per service
```

**Pass:** Each service 6/6 E2E; health URLs respond:

| Service | Health URL |
|---------|------------|
| sem-classifier | `http://localhost:8080/__health` |
| sem-scale-classifier | `http://localhost:8082/__health` |

## Dev — shared Authentik (once)

```bash
make infra-deploy
make configure-all
```

**Pass:** Namespace `authentik-reusable-ml-services` exists; Authentik pods Ready; each `infra-configure` creates a distinct OAuth2 application.

## Dev — single service

```bash
make render SERVICE=sem-classifier ENV=dev
make validate SERVICE=sem-classifier
make deploy SERVICE=sem-classifier DEPLOY_ARGS=--rebuild
make infra-configure SERVICE=sem-classifier
make access SERVICE=sem-classifier
make test-service SERVICE=sem-classifier
```

**Pass criteria:**

- All pods Ready in service namespace
- `curl http://localhost:8080/__health` → 200 (port from `service.yaml` → `dev_access.api_port`)
- `make test-service` → 6/6 passed

**Teardown / redeploy:**

```bash
make fresh SERVICE=sem-classifier
make test-service SERVICE=sem-classifier
```

## Dev — sem-scale-classifier (private cache)

Prerequisite: minimal HF cache at `model.cache_dir` in `service.yaml` (see [adding-a-service.md](adding-a-service.md)).

```bash
make fresh SERVICE=sem-scale-classifier
make access SERVICE=sem-scale-classifier
make test-service SERVICE=sem-scale-classifier
```

**Pass criteria:** 6/6 E2E on `http://localhost:8082`.

## Dev — coexistence

```bash
kubectl get pods -n sem-classifier
kubectl get pods -n sem-scale-classifier
kubectl get pods -n authentik-reusable-ml-services
curl -fsS http://localhost:8080/__health && curl -fsS http://localhost:8082/__health
```

## GHCR post-push checklist

- [ ] Image visible at `ghcr.io/<ghcr_owner>/<service-id>` (`ghcr_owner` in `ml_platform/config.yaml`)
- [ ] Package visibility set to **Public** on GitHub
- [ ] Release artifact under `services/<id>/releases/` references pushed digest

## Prod — preflight (no cluster required)

```bash
cp services/sem-classifier/prod.overlay.yaml.example services/sem-classifier/prod.overlay.yaml
# Fill kubernetes.namespace, auth URLs; secrets live outside repo

make render-prod SERVICE=sem-classifier
make verify-prod SERVICE=sem-classifier
make prod-pack SERVICE=sem-classifier
```

**Pass:** `verify-prod` exits 0; no `REPLACE_WITH_` in generated YAML/JSON; `auth.jwk_url` and `auth.issuer` use HTTPS; `disable_jwk_security` is false.

Repeat for `sem-scale-classifier` or use `make render-prod-all && make prod-pack-all`.

## Prod — optional dry-run (dev cluster)

```bash
export KUBECONFIG=/path/to/kubeconfig
export NS=sem-classifier
for f in services/sem-classifier/generated/prod/k8s/*.yaml; do
  kubectl apply --dry-run=server -n "$NS" -f "$f"
done
```

## Makefile reference

| Target | Purpose |
|--------|---------|
| `make check-prereqs` | podman, kubectl, GHCR_TOKEN |
| `make test-all` | E2E all services |
| `make verify-prod SERVICE=x` | Prod bundle gate |
| `make status SERVICE=x` | Pod status via app.sh |
