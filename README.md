# SEM Image Classifier API

Async image-classification API for Scanning Electron Microscopy (SEM) images. The default service serves a Hugging Face ViT image classifier through BentoML, queues work in Redis, exposes stable REST endpoints through KrakenD, and records authenticated API usage in PostgreSQL.

The repository is intentionally structured so another image or non-LLM model can reuse the same Kubernetes, gateway, queue, and reporting layers with minimal changes.

## Architecture

```text
Client
  |
  | JWT + REST
  v
KrakenD gateway
  |-- validates JWTs
  |-- maps public /api/v1/* routes to BentoML
  |-- records request usage in PostgreSQL
  v
BentoML service
  |-- accepts image upload or image_url
  |-- stores async jobs in Redis
  |-- runs SEM classifier inference in a worker thread
  v
Redis queue/results

PostgreSQL stores request-level usage rows for admin reports.
mock-oidc is included for local/dev JWT testing.
```

## Public API

All public routes are served through KrakenD on port `8080`.

| Method | Endpoint | Auth | Description |
| --- | --- | --- | --- |
| `GET` | `/__health` | No | KrakenD gateway health |
| `GET` | `/health` | No | BentoML model and Redis health |
| `POST` | `/api/v1/inference` | Yes | Submit an image inference job |
| `POST` | `/api/v1/jobs/status` | Yes | Poll job status |
| `POST` | `/api/v1/jobs/results` | Yes | Fetch completed job result |
| `GET` | `/api/v1/version` | No | Static API version/discovery response |

Status and result routes use `POST` because BentoML API methods receive JSON bodies. The public API stays stable even if the internal BentoML service class changes.

## Repository Layout

```text
sem-image-classifier-api/
├── Containerfile                  # Multi-stage CPU-only BentoML image build
├── README.md                      # Public project guide
├── LICENSE                        # EUPL-1.2 license text
├── NOTICE                         # Project attribution notice
├── pyproject.toml                 # Python project metadata and dependencies
├── db/schema.sql                  # PostgreSQL api_usage schema
├── docs/MODEL_MIGRATION.md        # How to adapt this repo to another model
├── gateway/                       # KrakenD flexible configuration and plugin
├── k8s/dev.sh                     # Build, deploy, access, and admin helper
├── k8s/env/                       # Tracked dev/prod templates and ignored local overrides
├── k8s/manifests/                 # Plain Kubernetes manifests in dependency order
├── scripts/usage_report.py        # Terminal, JSON, and HTML usage reports
├── scripts/stress_test_api.py     # Authenticated traffic generator
├── src/service.py                 # SEM-specific model loading and inference
├── src/image_service.py           # Reusable image input layer
├── src/model_service.py           # Reusable async queue/service foundation
├── src/redis_queue.py             # Payload-agnostic Redis job queue
└── tests/test_api.py              # End-to-end API smoke test
```

## Requirements

- Python 3.12
- `uv` for Python dependency management
- `podman` for image builds
- `kubectl` for Kubernetes operations
- SSH access to the target K3s control-plane host
- A container registry reachable by both the build host and Kubernetes nodes

Private cluster details belong in ignored local override files, not in tracked files.

## Configuration Model

`k8s/dev.sh` is the namespace and deployment authority. It owns the service identity and operational flow: app name, namespace, image repository, image tag, BentoML service entrypoint, namespace reset guardrails, build, deploy, access, and reporting helpers.

The project uses one public identity everywhere:

```bash
APP_NAME=sem-image-classifier
SERVICE_NAME=sem-image-classifier
NAMESPACE=sem-image-classifier
IMAGE_REPOSITORY=sem-image-classifier
BENTOML_SERVICE=service:SEMInferenceRedisService
```

Configuration is split into three domains:

| File | Owns | Should not contain |
| --- | --- | --- |
| `k8s/dev.sh` | Service identity, BentoML service entrypoint, build/deploy/reset/access behavior | Private IPs, credentials, model IDs |
| `k8s/env/<env>/bentoml-config*.yaml` | Redis connection, job TTL, model source, model ID, revision, cache directory | Cluster hostnames, registry, service identity |
| `k8s/env/<env>/cluster.env` and ignored `cluster.local.env` | K3s host/user/nodes, registry endpoint, remote kubeconfig path | App name, namespace, image repository, BentoML service, `MODEL_*` values |

Tracked `cluster.env` files provide example infrastructure defaults. For a real environment, create an ignored local infrastructure override:

```bash
# k8s/env/dev/cluster.local.env
K3S_API_HOST=your-k3s-host.example.org
K3S_SSH_USER=root
K3S_REMOTE_KUBECONFIG=/etc/rancher/k3s/k3s.yaml
K3S_NODES="your-k3s-host.example.org"
REGISTRY=registry.example.org:5000
REGISTRY_SCHEME=https
```

Use `./dev.sh config --env dev` to see the exact resolved files and non-secret values before running a build or namespace reset.

Tracked config files under `k8s/env/<env>/` remain templates. Machine-specific overrides use `.local.yaml`, `.local.json`, or `cluster.local.env`; these are ignored by git. `cluster.local.env` is intentionally restricted to infrastructure keys so it cannot create a second service identity.

## Model Source Contract

The container build supports two model sources:

| `MODEL_SOURCE` | Required values | Meaning |
| --- | --- | --- |
| `hugging_face` | `MODEL_ID`, `MODEL_REVISION` | Download a public Hugging Face model revision during image build and bake it into the image. |
| `private` | `MODEL_ID`, absolute `MODEL_CACHE_DIR` | Copy a local Hugging Face cache root during image build and validate the requested model offline. |

Private mode treats `MODEL_CACHE_DIR` as a Hugging Face cache root, not as a single snapshot directory. If `MODEL_REVISION` is omitted and exactly one snapshot exists for `MODEL_ID`, the build resolves that snapshot. If multiple snapshots exist, set `MODEL_REVISION` explicitly.

Runtime loads from the baked image cache. `MODEL_CACHE_DIR` is build-only.

## Build And Deploy

Set up local cluster values first:

```bash
cp k8s/env/dev/bentoml-config.yaml k8s/env/dev/bentoml-config.local.yaml
cp k8s/env/dev/gateway-settings.json k8s/env/dev/gateway-settings.local.json
cp k8s/env/dev/secrets.yaml k8s/env/dev/secrets.local.yaml
cat > k8s/env/dev/cluster.local.env <<'ENV'
K3S_API_HOST=your-k3s-host.example.org
K3S_SSH_USER=root
K3S_REMOTE_KUBECONFIG=/etc/rancher/k3s/k3s.yaml
K3S_NODES="your-k3s-host.example.org"
REGISTRY=registry.example.org:5000
REGISTRY_SCHEME=https
ENV
```

Then build and deploy:

```bash
cd k8s
./dev.sh config --env dev
./dev.sh access --env dev
./dev.sh build-image --env dev
./dev.sh bootstrap --env dev
./dev.sh access --env dev
```

`bootstrap` builds and pushes the image first, writes a release artifact, then resets the configured namespace and deploys from the release artifact. Protected namespaces such as `default`, `kube-system`, and `storage-system` are refused.

Useful operations:

```bash
cd k8s
./dev.sh status
./dev.sh logs bentoml
./dev.sh logs krakend
./dev.sh restart bentoml --env dev
./dev.sh token testuser
```

## Example API Session

With `./dev.sh access --env dev` running local forwards:

```bash
TOKEN=$(cd k8s && ./dev.sh token testuser)

curl -s http://localhost:8080/health | python3 -m json.tool

curl -s -X POST http://localhost:8080/api/v1/inference \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"image_url":"https://example.org/path/to/sem-image.jpg"}' \
  | python3 -m json.tool

curl -s -X POST http://localhost:8080/api/v1/jobs/status \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"job_id":"<job-id>"}' \
  | python3 -m json.tool

curl -s -X POST http://localhost:8080/api/v1/jobs/results \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"job_id":"<job-id>"}' \
  | python3 -m json.tool
```

The service accepts either `image_url` JSON input or multipart file upload with field name `image`.

## Usage Reports

`scripts/usage_report.py` reads the `api_usage` table from PostgreSQL through `kubectl exec` and produces admin-friendly usage views.

Modes:

| Mode | Purpose |
| --- | --- |
| `summary` | Fast terminal inspection. Defaults to the last 24 hours. |
| `report` | Static self-contained HTML dashboard. Defaults to all retained database history. |
| `json` | Machine-readable support output for automation. |

Common flags:

| Flag | Meaning |
| --- | --- |
| `--namespace` | Kubernetes namespace containing `postgresql-0`. Defaults to `sem-image-classifier`. |
| `--since` | Start time, ISO timestamp, or relative window such as `24h`, `7d`, `30d`. |
| `--until` | End time, ISO timestamp, relative window, or `now`. |
| `--timezone` | Display timezone for buckets and recent rows. Defaults to `UTC`. |
| `--bucket` | `auto`, `minute`, `hour`, or `day`. Defaults to `auto`. |
| `--recent-limit` | Number of recent rows to include. |
| `--output` | Destination for `report` or `json`. |

Examples:

```bash
# Quick operational check for the last day
python scripts/usage_report.py summary --namespace sem-image-classifier --since 24h

# Full retained-history HTML dashboard
python scripts/usage_report.py report \
  --namespace sem-image-classifier \
  --output /tmp/sem-usage-report.html

# Machine-readable output for another tool
python scripts/usage_report.py json \
  --namespace sem-image-classifier \
  --since 7d \
  --output /tmp/sem-usage.json
```

The HTML report is a single file with inline CSS/SVG and no JavaScript dependency. It includes coverage, KPI cards, traffic timeline, endpoint mix, status health, user leaderboard, hourly heatmap, recent requests, and data-quality notes.

## Stress Traffic

Use the stress script to seed usage data and test the gateway path:

```bash
python scripts/stress_test_api.py \
  --base-url http://localhost:8080 \
  --mock-token-url http://localhost:18080/default/token \
  --users alice,bob,charlie \
  --requests 30 \
  --concurrency 5 \
  --mode mixed \
  --poll
```

## Adapting To Another Model

Start with `docs/MODEL_MIGRATION.md`. The short version:

1. Keep `src/model_service.py`, `src/redis_queue.py`, gateway config, and Kubernetes manifests unless the serving pattern changes.
2. Replace or edit `src/service.py` with the new model loading, preprocessing, inference, and result schema.
3. Reuse `src/image_service.py` for image models, or create a sibling input layer for another payload type.
4. Edit `BENTOML_SERVICE` in `k8s/dev.sh` only if the BentoML entrypoint changes.
5. Update model env values in `k8s/env/<env>/bentoml-config*.yaml`.
6. Run the build, smoke test, stress test, and usage reports.

## CPU-Only Default

The default image uses CPU-only PyTorch wheels to keep the deployment smaller and simpler. This is the right default for single-image SEM inference. GPU support should be treated as a separate deployment design because it changes node prerequisites, image base, dependency resolution, scheduling, and resource limits.

## License

Copyright is held by AREA Science Park. The author is Luis Fernando Palacios Flores.

Licensed under the European Union Public Licence, version 1.2 or later (`EUPL-1.2-or-later`). See `LICENSE` and `NOTICE`.
