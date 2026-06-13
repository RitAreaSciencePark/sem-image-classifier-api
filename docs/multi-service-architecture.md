# Multi-Service Platform Architecture

## Overview

This repository is a **codegen-driven ML serving platform**. Each deployed API is defined by a hand-written `services/<id>/service.yaml`. A shared generator renders identical Kubernetes shapes for **dev** (Stencil K3s) and **prod** (AREA Science Park manual kubectl).

```
services/<id>/service.yaml          # identity, model, OIDC, dev ports
services/<id>/prod.overlay.yaml     # prod-only deltas (gitignored locally)
services/<id>/secrets.local.yaml    # cryptographic material (gitignored)
        │
        ▼  make render ENV=dev|prod
services/<id>/generated/{dev,prod}/
        │
        ├─ dev  → make deploy → k8s/app.sh (automated)
        └─ prod → make verify-prod → operator kubectl apply-order.txt
```

Shared identity infrastructure (Authentik) lives in namespace `authentik-reusable-ml-services` — one Authentik, many OAuth2 applications.

## Repository layout

| Path | Role |
|------|------|
| `src/core/` | Stable async pipeline (Redis queue, image fetch, classification base) |
| `src/models/` | Per-model BentoML services (`sem_classifier`, `sem_scale_classifier`) |
| `ml_platform/generator/` | `render.py`, `derive.py`, `validate.py`, `verify_prod.py`, `scaffold.py` |
| `ml_platform/templates/` | Jinja2 templates for K8s manifests, gateway settings, prod bundle |
| `ml_platform/config.yaml` | Platform defaults (`ghcr_owner`, `infra_namespace`) |
| `services/<id>/` | Per-service source of truth and generated artifacts |
| `gateway/` | KrakenD flexible configuration (shared across services) |
| `k8s/app.sh` | **Dev only** — build, push to GHCR, deploy |
| `k8s/infra.sh` | **Dev only** — shared Authentik stack |
| `Makefile` | Thin CLI over render, deploy, test, prod verify |

## Initial services

| Service | Namespace | Dev API / Auth PF | Model source |
|---------|-----------|-------------------|--------------|
| `sem-classifier` | `sem-classifier` | 8080 / 9001 | Hugging Face public |
| `sem-scale-classifier` | `sem-scale-classifier` | 8082 / 9002 | Private HF cache at build time |

## Dev vs prod rendering

| Artifact | `generated/dev/` | `generated/prod/` |
|----------|------------------|-------------------|
| `k8s/*.yaml` | GHCR image, dev ingress optional | GHCR image, prod ingress from overlay |
| `gateway-settings.json` | In-cluster HTTP JWKS workaround | External HTTPS JWKS from overlay |
| `gateway-settings.dev-workaround.json` | Yes | No |
| `deploy.env` | Ports, registry, OIDC vars | NS, image ref (no port-forwards) |
| `apply-order.txt` | Reference | Operator apply sequence |
| `preflight-checklist.md` | Dev checks | Prod checks |

Dev and prod use the same image path: `ghcr.io/<ghcr_owner>/<service-id>` (see `ml_platform/config.yaml`). Prod overlay may override owner, tag, or digest.

## Python extension model

Add model logic under `src/models/`. Reference it from `service.yaml`:

```yaml
model:
  module: models.my_model
  class: MyModelService
  bento_name: my-model
  source: hugging_face   # or private
  id: org/repo
  revision: <commit>
  cache_dir: ""          # absolute path required when source=private
```

Run `make validate SERVICE=<id>` to check schema + Python syntax before deploy.

## Gateway and database

- Gateway templates are parameterized by `display_name` and `service_id` — no hard-coded SEM strings in generated settings.
- `db/schema.sql` is generic usage tracking; applied via `postgresql-init` ConfigMap at deploy time.
- KrakenD ConfigMaps must exist **before** PostgreSQL and KrakenD rollouts (`app.sh` generates them first).

## Makefile targets

```bash
make render SERVICE=x ENV=dev          # Dev artifacts (default)
make render-prod SERVICE=x             # Prod bundle (requires prod.overlay.yaml)
make validate SERVICE=x                # Schema + Python import check
make deploy SERVICE=x                  # render + build + app.sh deploy
make deploy SERVICE=x DEPLOY_ARGS=--rebuild   # Force image rebuild
make test-service SERVICE=x            # E2E via tests/test_api.py
make infra-deploy                      # Shared Authentik (once)
make infra-configure SERVICE=x         # OIDC app per service
make verify-prod SERVICE=x             # Prod preflight gate
make prod-pack SERVICE=x               # Tarball for operator handoff
make teardown SERVICE=x                # Delete app namespace (dev)
```

See [adding-a-service.md](adding-a-service.md) for the five-command new-service flow.


## Maintainer touch surface

| Hand-written | Automated |
|--------------|-----------|
| `services/<id>/service.yaml` | `services/<id>/generated/**` |
| `src/models/<name>.py` | K8s manifests, gateway JSON, `deploy.env` |
| `services/<id>/secrets.local.yaml` | `make render`, `make deploy`, `make test-all` |
| `services/<id>/prod.overlay.yaml` | `make verify-prod`, `make prod-pack` |
| `ml_platform/config.yaml` (`ghcr_owner`) | `make render-all` after fork |

Fork handoff: change `ghcr_owner` in `ml_platform/config.yaml`, then `make render-all`.
