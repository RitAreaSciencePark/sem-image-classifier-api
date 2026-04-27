# Model Migration Guide

This repository is a concrete SEM image classifier and a reusable async model-serving skeleton. The goal of a migration is to change only the model-specific surface while preserving the queue, gateway, Kubernetes, and reporting machinery.

## 1. Decide What Kind Of Model You Are Serving

Use the existing shape when the new model is an image classifier or image model:

- Keep `src/image_service.py` for image upload and `image_url` support.
- Replace SEM-specific loading and inference in `src/service.py`.
- Keep the public API routes unchanged unless the product intentionally changes.

Create a new input layer only when the payload is not an image:

- Text model: create a text input service beside `image_service.py`.
- Tabular model: create a JSON/table input service.
- File model: create a file serialization layer with size limits.

Do not modify Redis, KrakenD, PostgreSQL, or Kubernetes just because the model changed.

## 2. Update The BentoML Service

`src/service.py` should contain the model-specific code:

- BentoML service class and service name.
- Model loading and cache behavior.
- Preprocessing.
- Inference.
- Result schema.

The reusable layers should stay generic:

- `src/model_service.py`: async queue lifecycle, worker loop, status, results, health.
- `src/image_service.py`: PIL/image URL handling and serialization.
- `src/redis_queue.py`: payload-agnostic Redis queue.

If the service class or module changes, edit `BENTOML_SERVICE` in `k8s/dev.sh`:

```bash
BENTOML_SERVICE=service:YourBentoMLServiceClass
```

The `Containerfile` receives this through `dev.sh` and starts that BentoML entrypoint. Do not put `BENTOML_SERVICE` in `cluster.local.env`; that file is only for private infrastructure facts.

## 3. Update Model Configuration

Edit `k8s/env/<env>/bentoml-config.yaml` or an ignored `.local.yaml` override.

Public Hugging Face model:

```yaml
MODEL_SOURCE: "hugging_face"
MODEL_ID: "org/model-name"
MODEL_REVISION: "<tag-or-commit-sha>"
MODEL_CACHE_DIR: ""
```

Private Hugging Face cache:

```yaml
MODEL_SOURCE: "private"
MODEL_ID: "org/model-name"
MODEL_REVISION: ""  # optional if exactly one cached snapshot exists
MODEL_CACHE_DIR: "/absolute/path/to/huggingface/cache/root"
```

`MODEL_CACHE_DIR` is build-only. The runtime container loads from the baked Hugging Face cache.

## 4. Keep Deployment Identity In `dev.sh`

For a new product, change identity constants in `k8s/dev.sh`:

```bash
APP_NAME=my-model-api
SERVICE_NAME=my-model-api
NAMESPACE=my-model-api
IMAGE_REPOSITORY=my-model-api
BENTOML_SERVICE=service:MyModelService
```

`SERVICE_NAME` is recorded in PostgreSQL usage rows. `APP_NAME` and `NAMESPACE` control Kubernetes identity. `IMAGE_REPOSITORY` controls the pushed container image name.

`cluster.local.env` remains intentionally narrower:

```bash
K3S_API_HOST=your-k3s-host.example.org
K3S_SSH_USER=root
K3S_NODES="your-k3s-host.example.org"
REGISTRY=registry.example.org:5000
REGISTRY_SCHEME=https
```

Keeping identity out of local infrastructure files prevents two names for the same stack.

## 5. Keep Gateway Routes Stable Unless Product Requirements Change

The default public routes are:

- `POST /api/v1/inference`
- `POST /api/v1/jobs/status`
- `POST /api/v1/jobs/results`
- `GET /health`
- `GET /api/v1/version`

If the new model still follows async job submission, keep these routes. Change the route contract only when clients need a different API shape.

## 6. Validate The Migration

Run the smallest checks first:

```bash
python -m py_compile src/*.py scripts/*.py
bash -n k8s/dev.sh
git diff --check
```

Then validate Kubernetes and the image build:

```bash
kubectl apply --dry-run=client --validate=false -n "$NAMESPACE" \
  -f k8s/env/dev/secrets.yaml \
  -f k8s/env/dev/bentoml-config.yaml \
  -f k8s/manifests/01-redis.yaml \
  -f k8s/manifests/02-bentoml.yaml \
  -f k8s/manifests/03-postgresql.yaml \
  -f k8s/manifests/04-mock-oidc.yaml \
  -f k8s/manifests/05-krakend.yaml \
  -f k8s/manifests/06-ingress.yaml \
  -f k8s/manifests/07-networkpolicies.yaml \
  -f k8s/manifests/08-hpa.yaml

cd k8s
./dev.sh build-image --env dev
./dev.sh bootstrap --env dev
./dev.sh access --env dev
```

Finally test the end-to-end path:

```bash
python tests/test_api.py
python scripts/stress_test_api.py --requests 30 --concurrency 5 --mode mixed --poll
python scripts/usage_report.py summary --since 24h
python scripts/usage_report.py report --output /tmp/model-usage-report.html
```

## Migration Rule Of Thumb

If a change is about model semantics, put it in `src/service.py` or a new input service. If a change is about model selection, put it in `bentoml-config*.yaml`. If a change is about deployment identity or BentoML entrypoint, put it in `k8s/dev.sh`. If a change is about private cluster access, put it in `cluster.local.env`. If a change is about queueing, auth, or reporting, pause and verify that the serving pattern truly changed before editing shared infrastructure.
