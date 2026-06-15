# Adding a New Service

Single entry point for onboarding a new ML API on this platform.

## Where to look

| What | File | Notes |
|------|------|-------|
| Service identity, dev namespace, model, OIDC, ports | `services/<id>/service.yaml` | **Hand-written** |
| Model logic | `src/models/<module>.py` | **Hand-written** |
| Dev/prod passwords | `services/<id>/secrets.local.yaml` | Auto-created by `make onboard`; set `oidc_client_secret` manually |
| Prod namespace, external auth, ingress | `services/<id>/prod.overlay.yaml` | Copy from `.example`; prod namespace is platform-assigned |
| Worker batch size, HPA, gateway defaults | `ml_platform/config.yaml` | Platform-wide; rarely changed |
| K8s manifests, gateway JSON, ConfigMaps | `services/<id>/generated/` | **Never edit** — run `make render` |

## Quick start (one command + deploy)

```bash
make onboard SERVICE=my-api \
  MODEL_ID=org/model-name MODEL_SOURCE=hugging_face \
  API_PORT=8084 AUTHENTIK_PORT=9004

# Edit src/models/my_api.py and services/my-api/service.yaml if needed
# Set oidc_client_secret in services/my-api/secrets.local.yaml (from Authentik after configure)

make deploy SERVICE=my-api DEPLOY_ARGS=--rebuild
make infra-configure SERVICE=my-api
make access SERVICE=my-api
make test-service SERVICE=my-api
```

`make onboard` scaffolds `service.yaml` (with `kubernetes.namespace` for dev), model stub, `secrets.local.yaml`, runs `make render`, and validates. Dev gateway JWT settings are generated automatically — no manual gateway file copy.

`make new-service` is a deprecated alias for `make onboard`.

## Namespaces

- **Dev:** set `kubernetes.namespace` in `service.yaml` (typically matches `service_id`).
- **Prod:** set `kubernetes.namespace` in `prod.overlay.yaml` — assigned by the platform operator, not inferred from `service_id`.

## Port allocation (dev)

Avoid collisions with other projects on the Stencil cluster:

| Service | API | Authentik PF |
|---------|-----|--------------|
| sem-classifier | 8080 | 9001 |
| sem-scale-classifier | 8082 | 9002 |
| s3bucket (external) | 3000 | 9000 |

Set `dev_access.api_port` and `dev_access.authentik_port` in `service.yaml`.

## Private model cache builds

When `model.source: private`, set an **absolute** `model.cache_dir` pointing at a Hugging Face cache root containing the model snapshot (not the full `~/.cache/huggingface` tree — keep the context minimal).

```yaml
model:
  source: private
  id: t0m-R/vit-sem-scale-classifier
  revision: a20e54a100db0a8a4f9bba0356247bbe5d486593
  cache_dir: /root/private-model-registry/sem-scale-cache
```

Build copies only that directory into the image via Containerfile `model_cache` build context.

## Production

```bash
cp services/my-api/prod.overlay.yaml.example services/my-api/prod.overlay.yaml
# Fill kubernetes.namespace, jwk_url, issuer, ingress.host

make render-prod SERVICE=my-api
make verify-prod SERVICE=my-api    # MUST pass before kubectl
make prod-pack SERVICE=my-api      # Hand off generated/prod-bundle.tar.gz
```

Apply using `services/my-api/generated/prod/apply-order.txt`. See [production-deployment.md](production-deployment.md).

After deploy, generate an HTML usage report:

```bash
cd services/my-api/generated/prod/usage-report
./run.sh --namespace <prod-namespace> --since 24h --output-dir ./reports
```

See [usage-reports.md](usage-reports.md).

## Iteration protocol (dev)

After each major change:

```bash
make deploy SERVICE=x
make test-service SERVICE=x
make teardown SERVICE=x
make deploy SERVICE=x
make test-service SERVICE=x
```

Document exact commands in [deployment-verification.md](deployment-verification.md).
