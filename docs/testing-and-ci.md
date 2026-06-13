# Testing and CI

## Local quality gate

```bash
make verify SERVICE=sem-classifier   # validate + compileall
make check-prereqs                   # podman, kubectl, GHCR_TOKEN
```

## End-to-end (Stencil dev)

```bash
make access SERVICE=sem-classifier
make test-service SERVICE=sem-classifier
make test-all                        # all services; reads secrets.local.yaml
```

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
