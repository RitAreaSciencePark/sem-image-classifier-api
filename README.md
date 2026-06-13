# SEM Classifier API

Multi-service ML serving platform for Scanning Electron Microscopy (SEM) image classification. Each API is defined by `services/<id>/service.yaml`; a shared generator renders identical Kubernetes shapes for **dev** (Stencil K3s) and **prod** (manual kubectl).

Stack: **BentoML** + **Redis** + **KrakenD** + **PostgreSQL** usage tracking. JWT RS256 via shared Authentik (`authentik-reusable-ml-services`).

## Architecture

```mermaid
flowchart LR
  Client -->|JWT| KrakenD
  KrakenD --> BentoML
  BentoML --> Redis
  KrakenD --> PostgreSQL
  Authentik -->|M2M tokens| Client
```

| Service | Namespace | Dev ports (API / Auth PF) |
|---------|-----------|---------------------------|
| `sem-classifier` | `sem-classifier` | 8080 / 9001 |
| `sem-scale-classifier` | `sem-scale-classifier` | 8082 / 9002 |

## Quick start (dev)

```bash
cp k8s/.env.example k8s/.env          # set GHCR_TOKEN (write:packages PAT)
cp k8s/env/dev/cluster.local.env.example k8s/env/dev/cluster.local.env  # if needed

make check-prereqs
make infra-deploy
make deploy-all DEPLOY_ARGS=--rebuild
make configure-all
make access SERVICE=sem-classifier    # repeat per service or see docs
make test-all
```

## Registry handoff (forks)

1. Set `ghcr_owner` in [`ml_platform/config.yaml`](ml_platform/config.yaml).
2. `make render-all ENV=dev && make render-prod-all`
3. First `make deploy SERVICE=x DEPLOY_ARGS=--rebuild` creates the GHCR package.
4. Set each package to **Public** on GitHub (K3s pulls without `imagePullSecrets`).

Image path (dev = prod): `ghcr.io/<ghcr_owner>/<service-id>:latest`

## Repository layout

```text
sem-classifier-api/
├── ml_platform/           # Generator, templates, config.yaml
├── services/<id>/         # service.yaml, secrets, prod.overlay.yaml
│   └── generated/         # Rendered dev/prod artifacts (do not edit)
├── src/core/              # Shared pipeline
├── src/models/            # Per-model BentoML services
├── gateway/               # KrakenD flexible config + usage plugin
├── k8s/app.sh             # Dev deploy only
├── k8s/infra.sh           # Shared Authentik (dev)
├── Makefile               # Primary operator interface
└── docs/README.md         # Documentation index
```

## Makefile targets

| Target | Purpose |
|--------|---------|
| `make render SERVICE=x` | Generate `services/x/generated/dev/` |
| `make deploy SERVICE=x DEPLOY_ARGS=--rebuild` | Render, build, push GHCR, deploy |
| `make fresh SERVICE=x` | Delete namespace + rebuild + configure |
| `make test-all` | E2E all services (reads `secrets.local.yaml`) |
| `make render-prod SERVICE=x` | Generate prod bundle |
| `make verify-prod SERVICE=x` | Prod preflight (must pass) |
| `make prod-pack SERVICE=x` | Tarball for prod operator |

## Public API

KrakenD gateway (port from `service.yaml` `dev_access.api_port`, default 8080):

| Method | Endpoint | Auth |
|--------|----------|------|
| `GET` | `/__health` | No |
| `GET` | `/health` | No |
| `POST` | `/api/v1/inference` | JWT |
| `POST` | `/api/v1/jobs/status` | JWT |
| `POST` | `/api/v1/jobs/results` | JWT |
| `GET` | `/api/v1/version` | No |

## Adding a service

See [docs/adding-a-service.md](docs/adding-a-service.md). Four commands after editing `service.yaml` and model code:

```bash
make deploy SERVICE=my-api DEPLOY_ARGS=--rebuild
make infra-configure SERVICE=my-api
make access SERVICE=my-api
make test-service SERVICE=my-api
```

## Production

Production does **not** use `k8s/app.sh`. See [docs/production-deployment.md](docs/production-deployment.md):

```bash
make render-prod SERVICE=sem-classifier
make verify-prod SERVICE=sem-classifier
make prod-pack SERVICE=sem-classifier
# Operator applies generated/prod/apply-order.txt
```

## Documentation

Full index: [docs/README.md](docs/README.md)
