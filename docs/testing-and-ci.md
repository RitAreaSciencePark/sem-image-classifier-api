# Testing and CI

## Local quality gate

```bash
make verify SERVICE=sem-classifier   # validate + compileall + unit tests + e2e collection
make test-unit                     # offline tests only (no cluster)
make check-prereqs                 # podman, kubectl, GHCR_TOKEN
```

Shared test/dev helpers: [`ml_platform/devtools/`](../ml_platform/devtools/).

## End-to-end (Stencil dev)

Prerequisites: deployed service, `make access SERVICE=<id>`, OIDC secret in `services/<id>/secrets.local.yaml`. Pass criteria: **6/6** tests per service — see [deployment-verification.md](deployment-verification.md).

```bash
make access SERVICE=sem-classifier
make test-service SERVICE=sem-classifier   # pytest tests/e2e -m e2e
make test-all                              # all services
```

See [`tests/README.md`](../tests/README.md) and [`scripts/README.md`](../scripts/README.md) for load tests and usage reports.

```bash
make stress-test SERVICE=sem-classifier
make usage-report SERVICE=sem-classifier   # last 24h HTML report → /tmp/
```

Usage reports in production: [usage-reports.md](usage-reports.md).

## Prod bundle gate

```bash
make render-prod-all
make verify-prod SERVICE=sem-classifier
make prod-pack-all
```

## Secrets matrix

| Secret | Location | Used by |
|--------|----------|---------|
| `GHCR_TOKEN` | `k8s/.env` (gitignored) | Image push on deploy host |
| OIDC client secret | `services/<id>/secrets.local.yaml` | E2E tests, Authentik |
| Infra bootstrap | `k8s/env/dev/infra-secrets.local.yaml` | `make infra-deploy` |
