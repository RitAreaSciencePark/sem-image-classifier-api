# Setting Up the Development Environment

This guide walks through deploying the ML API platform on a **Stencil K3s cluster** (validated on `192.168.132.0/24`). Read [dev-environment-overview.md](./dev-environment-overview.md) first for architecture context.

**Operator interface:** use `make` targets from the repo root. Shell scripts under `k8s/` are thin wrappers — always pass `--service <id>` when calling them directly.

---

## Prerequisites

**Hardware:** see [Host machine requirements](./dev-environment-overview.md#host-machine-requirements).

**Software on the host:** git, podman, kubectl, ssh (Fedora/RHEL: `dnf install git podman kubectl openssh-clients`).

**SSH key** at `~/.ssh/id_rsa` if you provision VMs from scratch.

**GitHub PAT:** classic token with `write:packages` for GHCR push → `k8s/.env` (see `k8s/.env.example`).

**Shared cluster shortcut:** if a Stencil K3s cluster is already running, skip to [Step 7](#step-7-repository-and-cluster-credentials).

---

## Steps 1–6: Provision the Stencil virtual datacenter (optional)

These steps create KVM VMs, Ceph, FreeIPA, and K3s from scratch. They are **shared infrastructure** used by multiple AREA Science Park projects.

1. Clone the four Stencil provisioning repositories (`tofu-libvirt`, `ceph-provisioning`, `freeipa-provisioning`, `kubernetes-provisioning`).
2. Configure `vars.json` — disk sizes, network addresses, SSH key path.
3. Provision VMs (OpenTofu) → Ceph (Ansible) → FreeIPA (Ansible) → K3s (Ansible).

**Detailed walkthrough:** [Bucket Explorer dev-environment-setup Steps 1–6](https://github.com/luisfpal/s3bucket_manager_app/blob/main/docs/dev-environment-setup.md#step-1-clone-the-stencil-repositories) (replace TEST-NET-2 `198.51.100.0/24` with your cluster block, e.g. `192.168.132.0/24`).

After Step 6:

```bash
kubectl get nodes   # all nodes Ready
```

---

## Step 7: Repository and cluster credentials

```bash
git clone <repo-url> sem-classifier-api && cd sem-classifier-api

cp k8s/.env.example k8s/.env
# Edit k8s/.env — set GHCR_TOKEN (write:packages PAT)
```

**Cluster SSH/tunnel settings** (gitignored local override):

```bash
cat > k8s/env/dev/cluster.local.env <<'ENV'
K3S_API_HOST=192.168.132.10
K3S_SSH_USER=root
K3S_REMOTE_KUBECONFIG=/etc/rancher/k3s/k3s.yaml
K3S_NODES="192.168.132.10 192.168.132.11 192.168.132.12"
ENV
```

Tracked defaults live in `k8s/env/dev/cluster.env`; `cluster.local.env` overrides them on your machine.

**Infra secrets** (Authentik bootstrap — once per cluster):

```bash
cp k8s/env/dev/infra-secrets.yaml k8s/env/dev/infra-secrets.local.yaml
# Fill bootstrap password and related values
```

**Per-service app secrets** (one file per API):

```bash
# If missing, copy from generated example after first render:
make render SERVICE=sem-classifier
cp services/sem-classifier/generated/dev/secrets.yaml.example \
   services/sem-classifier/secrets.local.yaml
# Edit REDIS_PASSWORD, POSTGRES_PASSWORD, oidc-client-secret as needed
```

Repeat for each service, or use `make onboard SERVICE=<id> MODEL_ID=...` for new services (creates `secrets.local.yaml` automatically).

See [k8s/env/README.md](../k8s/env/README.md) for file precedence.

---

## Step 8: Render, deploy, configure

```bash
make check-prereqs
make render-all ENV=dev

make infra-deploy                              # shared Authentik (once)
make deploy-all DEPLOY_ARGS=--rebuild          # build, push GHCR, apply manifests per service
make configure-all                             # OAuth2 app per service in Authentik
```

What happens:

1. **`make render`** generates `services/<id>/generated/dev/` (K8s YAML, gateway JSON, `deploy.env`, ConfigMaps inputs). **Do not hand-edit generated files.**
2. **`make deploy`** builds the container with podman, pushes to `ghcr.io/<ghcr_owner>/<service-id>`, applies manifests via `k8s/app.sh`.
3. After first push, set each GHCR package to **Public** on GitHub so K3s nodes can pull without credentials.
4. Release digest is pinned under `services/<id>/releases/release-latest.json`.
5. **`make infra-configure`** registers the OAuth2 client from `app-secrets` in the cluster Authentik.

**Gateway auth (dev):** no manual copy step. The generator renders `gateway-settings.json` with in-cluster Authentik HTTP JWKS (`disable_jwk_security: true`). Production uses external HTTPS from `prod.overlay.yaml`.

---

## Step 9: Access and verify

```bash
make access SERVICE=sem-classifier
make token SERVICE=sem-classifier
curl -s http://localhost:8080/__health

make test-all    # E2E all services (6 tests each; reads secrets.local.yaml)
```

Port numbers come from `services/<id>/service.yaml` → `dev_access.api_port` / `authentik_port`.

Manual equivalent:

```bash
make access SERVICE=sem-scale-classifier
curl -s http://localhost:8082/__health
make test-service SERVICE=sem-scale-classifier
```

Full checklists: [deployment-verification.md](./deployment-verification.md).

---

## Step 10: Coexistence with other projects

Multiple applications share one K3s cluster in isolated namespaces.

| Resource | sem-classifier | sem-scale-classifier | Shared infra |
|----------|----------------|----------------------|--------------|
| App namespace | `sem-classifier` | `sem-scale-classifier` | — |
| KrakenD port-forward | 8080 | 8082 | — |
| Authentik port-forward | 9001 | 9002 | NS: `authentik-reusable-ml-services` |
| K3s API tunnel | 16443 | 16443 | shared |

Check Authentik without touching app namespaces:

```bash
./k8s/infra.sh check
./k8s/infra.sh status
```

See [port allocation](./dev-environment-overview.md#port-allocation-on-the-developer-machine) before starting another project's port-forwards.

---

## Production path

Development scripts (`k8s/app.sh`, `k8s/infra.sh`) are **not** used in production. Operators apply `services/<id>/generated/prod/` using `apply-order.txt`. See [production-deployment.md](production-deployment.md).
