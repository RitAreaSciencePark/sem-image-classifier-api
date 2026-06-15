# Multi-Service Platform Architecture

## Overview

This repository is a **codegen-driven ML serving platform**. Each API is defined by hand-written `services/<id>/service.yaml`. A shared generator renders identical Kubernetes shapes for **dev** (Stencil K3s, automated) and **prod** (manual kubectl).

```
services/<id>/service.yaml          # identity, dev namespace, model, OIDC, ports
services/<id>/prod.overlay.yaml     # prod namespace, external auth, ingress (gitignored)
services/<id>/secrets.local.yaml    # Redis/Postgres/OIDC secrets (gitignored)
        │
        ▼  make render ENV=dev|prod
services/<id>/generated/{dev,prod}/
        │
        ├─ dev  → make deploy → k8s/app.sh
        └─ prod → make verify-prod → operator apply-order.txt
```

Shared Authentik lives in `authentik-reusable-ml-services` — one identity server, many OAuth2 applications (`make infra-configure SERVICE=<id>` per API).

## Repository layout

| Path | Role |
|------|------|
| `src/core/` | Async pipeline (Redis queue, image fetch, classification base) |
| `src/models/` | Per-model BentoML services |
| `ml_platform/generator/` | `render`, `derive`, `validate`, `verify_prod`, `scaffold` |
| `ml_platform/templates/` | Jinja2 → K8s, gateway JSON, prod bundle |
| `ml_platform/config.yaml` | Platform defaults (worker batch, HPA, gateway, `ghcr_owner`) |
| `services/<id>/` | Per-service source of truth + generated artifacts |
| `gateway/` | KrakenD flexible config + usage-tracking plugin |
| `k8s/app.sh` | **Dev only** — build, GHCR push, deploy |
| `k8s/infra.sh` | **Dev only** — shared Authentik |
| `Makefile` | Primary operator interface (`make help`) |

## Initial services

| Service | Dev namespace | Dev API / Auth PF | Model source |
|---------|---------------|-------------------|--------------|
| `sem-classifier` | `sem-classifier` | 8080 / 9001 | Hugging Face public |
| `sem-scale-classifier` | `sem-scale-classifier` | 8082 / 9002 | Private HF cache at build time |

Namespaces are **explicit** in YAML (`kubernetes.namespace`), not inferred from `service_id`.

## Dev vs prod rendering

| Artifact | `generated/dev/` | `generated/prod/` |
|----------|------------------|-------------------|
| `k8s/*.yaml` | GHCR `:latest`, optional dev ingress | Ingress + external auth from overlay |
| `gateway-settings.json` | In-cluster Authentik HTTP JWKS | External HTTPS JWKS from overlay |
| `deploy.env` | Ports, registry, OIDC vars | Namespace, image ref |
| `apply-order.txt` | Reference | Operator apply sequence |
| `preflight-checklist.md` | Dev checks | Prod gate checklist |

Both environments use `ghcr.io/<ghcr_owner>/<service-id>` unless overlay pins digest. Owner: `ml_platform/config.yaml`.

## Python extension model

Add logic under `src/models/`. Reference from `service.yaml`:

```yaml
kubernetes:
  namespace: my-api          # dev cluster namespace

model:
  module: models.my_model
  class: MyModelService
  bento_name: my-model
  source: hugging_face       # or private
  id: org/repo
  revision: <commit>
  cache_dir: ""              # absolute path when source: private
```

Run `make validate SERVICE=<id>` before deploy.

## Gateway and database

- Gateway templates parameterize `display_name` and `service_id` — no hard-coded SEM strings.
- `db/schema.sql` defines usage tracking; applied via `postgresql-init` ConfigMap at deploy.
- KrakenD ConfigMaps are rendered **before** PostgreSQL/KrakenD rollouts (`app.sh` order).

## Makefile targets

Run `make help` for the full list. Core targets:

```bash
make onboard SERVICE=x MODEL_ID=org/model   # new service
make render SERVICE=x                       # dev artifacts
make render-prod SERVICE=x                  # prod bundle
make validate SERVICE=x                     # schema + Python check
make deploy SERVICE=x DEPLOY_ARGS=--rebuild # render + build + deploy
make fresh SERVICE=x                        # delete NS + rebuild + configure
make infra-deploy                           # shared Authentik (once)
make infra-configure SERVICE=x              # OAuth2 app for service
make access SERVICE=x                       # tunnel + port-forwards
make test-service SERVICE=x                 # E2E (6 tests)
make verify-prod SERVICE=x                  # prod preflight gate
make prod-pack SERVICE=x                    # tarball handoff
make teardown SERVICE=x                     # delete app namespace only
```

Multi-service: `make render-all`, `make deploy-all`, `make fresh-all`, `make test-all` — see `make help-advanced`.

See [adding-a-service.md](adding-a-service.md) for onboarding.

## Maintainer touch surface

| Hand-written | Never edit (regenerate) |
|--------------|-------------------------|
| `services/<id>/service.yaml` | `services/<id>/generated/**` |
| `src/models/<name>.py` | K8s manifests, gateway JSON, `deploy.env` |
| `services/<id>/secrets.local.yaml` | |
| `services/<id>/prod.overlay.yaml` | |
| `ml_platform/config.yaml` | |

After fork: change `ghcr_owner` in `ml_platform/config.yaml`, then `make render-all && make render-prod-all`.

Worker and HPA tuning: [inference-workers.md](inference-workers.md), [autoscaling.md](autoscaling.md).
