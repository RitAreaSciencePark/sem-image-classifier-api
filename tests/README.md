# Tests

## Layout

| Directory | Purpose | Cluster required |
|-----------|---------|------------------|
| [`unit/`](unit/) | Offline tests for `ml_platform/devtools` | No |
| [`e2e/`](e2e/) | Gateway contract tests (`@pytest.mark.e2e`) | Yes |

Shared fixtures live in [`conftest.py`](conftest.py). E2E tests import from `ml_platform/devtools` — never from `scripts/`.

## Prerequisites (E2E)

1. Shared Authentik: `make infra-deploy` and `make infra-configure SERVICE=<id>`
2. Service deployed: `make deploy SERVICE=<id>`
3. Port-forwards active: `make access SERVICE=<id>`
4. OIDC secret in `services/<id>/secrets.local.yaml` (or `AUTH_CLIENT_SECRET` in env)

Optional per-service overrides: [`services/<id>/e2e.yaml`](../services/sem-classifier/e2e.yaml) (`test_image_url`, `inference_timeout_s`, `expected_labels`).

See [dev-environment-setup.md](../docs/dev-environment-setup.md) or [deployment-verification.md](../docs/deployment-verification.md) for cluster setup and pass criteria.

## Run

```bash
make verify                          # validate + compile + unit tests + e2e collection
make test-unit
make test-service SERVICE=sem-classifier
make test-all

# Direct pytest
pytest tests/unit -v
SERVICE=sem-scale-classifier pytest tests/e2e -v -m e2e
```

E2E preflight checks `{base_url}/__health` before acquiring a token. If the gateway is unreachable, pytest exits with an actionable message.
