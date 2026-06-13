# Dev deployment operations

## Mental model

| Layer | Namespace | Tooling |
|-------|-----------|---------|
| Shared Authentik | `authentik-reusable-ml-services` | `make infra-deploy` |
| Per-service API | `services/<id>/` name (e.g. `sem-classifier`) | `make deploy SERVICE=<id>` |
| Container images | `ghcr.io/<ghcr_owner>/<service-id>` | `make deploy DEPLOY_ARGS=--rebuild` |

## Requirements

| Check | Verify | Fix |
|-------|--------|-----|
| GHCR push token | `make check-prereqs` | Copy `k8s/.env.example` → `k8s/.env`, set `GHCR_TOKEN` |
| GHCR owner | `grep ghcr_owner ml_platform/config.yaml` | Edit one line, `make render-all` |
| K3s access | `make access SERVICE=<id>` | Fill `k8s/env/dev/cluster.local.env` |
| GHCR pull (K3s) | Pods `Running` | Set package **Public** on GitHub after first push |

## Redeploy classes

**Class A — config only:** `make render SERVICE=x && make deploy SERVICE=x`

**Class B — new image:** `make deploy SERVICE=x DEPLOY_ARGS=--rebuild`

**Class C — clean namespace:** `make fresh SERVICE=x` (delete NS + rebuild + `infra-configure`)

**All services:** `make fresh-all` then `make test-all`

## Ports (default services)

| Service | KrakenD | Authentik PF |
|---------|---------|--------------|
| sem-classifier | 8080 | 9001 |
| sem-scale-classifier | 8082 | 9002 |

Run `make access SERVICE=<id>` per service (or SSH tunnel with both port pairs).
