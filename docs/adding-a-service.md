# Adding a New Service

## What you write by hand

1. **`services/<id>/service.yaml`** — identity, model, OIDC, dev port allocation
2. **`src/models/<module>.py`** — BentoML service class extending `ImageClassificationModelService`
3. **`services/<id>/secrets.local.yaml`** — passwords + OIDC client secret (copy from generated example)
4. **`services/<id>/prod.overlay.yaml`** — prod hostnames and external Authentik URLs (copy from `.example`)

Everything else is generated.

## Quick start (five commands)

```bash
# 1. Scaffold service.yaml + model stub
make new-service SERVICE=my-api \
  MODEL_ID=org/model-name MODEL_SOURCE=hugging_face \
  API_PORT=8084 AUTHENTIK_PORT=9004

# 2. Implement src/models/my_api.py and edit services/my-api/service.yaml

# 3. Render + validate
make render SERVICE=my-api && make validate SERVICE=my-api

# 4. Secrets (once)
cp services/my-api/generated/dev/secrets.yaml.example \
   services/my-api/secrets.local.yaml
# Edit CHANGE_ME_* values

# 5. Deploy (dev)
make deploy SERVICE=my-api DEPLOY_ARGS=--rebuild
make infra-configure SERVICE=my-api
make access SERVICE=my-api
make test-service SERVICE=my-api   # reads secrets.local.yaml
```

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
# Fill jwk_url, issuer, ingress.host (6–8 values)

make render-prod SERVICE=my-api
make verify-prod SERVICE=my-api    # MUST pass before kubectl
make prod-pack SERVICE=my-api      # Hand off generated/prod-bundle.tar.gz
```

Apply using `services/my-api/generated/prod/apply-order.txt`. See [production-deployment.md](production-deployment.md).

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
