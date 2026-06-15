# k8s/env — cluster infrastructure only

Per-service config (BentoML, gateway, app secrets) lives under `services/<id>/` and is **generated** by `make render`. Do not add app-level config here.

## What remains here

| Path | Purpose |
|------|---------|
| `dev/cluster.env` | Tracked K3s SSH/tunnel defaults |
| `dev/cluster.local.env` | Gitignored machine overrides (`K3S_API_HOST`, `K3S_NODES`, …) |
| `dev/infra-secrets.yaml` | Authentik bootstrap secrets template |
| `dev/infra-secrets.local.yaml` | Gitignored real infra bootstrap values |

**Precedence:** `cluster.local.env` overrides `cluster.env`. Create `cluster.local.env` manually — there is no `.example` file.

## Deploy flow

```bash
make infra-deploy                              # Authentik → authentik-reusable-ml-services
make deploy SERVICE=sem-classifier DEPLOY_ARGS=--rebuild
make infra-configure SERVICE=sem-classifier   # OAuth2 app for this service
```

App secrets: `services/<id>/secrets.local.yaml` (not under `k8s/env/`).

See [docs/dev-deployment-operations.md](../docs/dev-deployment-operations.md).
