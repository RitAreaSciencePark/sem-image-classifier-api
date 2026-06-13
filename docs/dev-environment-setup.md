# Setting Up the Development Environment

This guide walks through deploying the SEM Image Classifier API on the Stencil virtual datacenter. A person who completes these steps will have an environment matching the one used to develop and validate this project.

Before starting: read [dev-environment-overview.md](./dev-environment-overview.md) to understand what you are building and why.

---

## Prerequisites

**Hardware:**

See [dev-environment-overview.md#host-machine-requirements](./dev-environment-overview.md#host-machine-requirements) for the full rationale. Summary:

| Component | Minimum | Recommended |
|-----------|---------|-------------|
| CPU | 8 physical cores (VT-x/AMD-V required) | 16+ cores |
| RAM | 64 GB | 128 GB |
| Disk | 300 GB SSD | 500 GB NVMe |

Linux host OS is required — Fedora or RHEL/CentOS-compatible recommended.

**Software on the host:**

```bash
# Fedora/RHEL
sudo dnf install -y git opentofu libvirt libvirt-client qemu-kvm \
    wget ansible python3 helm kubectl podman

sudo systemctl enable --now libvirtd
sudo usermod -aG libvirt $(whoami)
```

Minimum versions: OpenTofu ≥ 1.6, Ansible ≥ 2.15, Helm ≥ 3.12, Python ≥ 3.10.

**SSH key** at `~/.ssh/id_rsa` (provisioning tools expect RSA):

```bash
ls ~/.ssh/id_rsa.pub || ssh-keygen -t rsa -b 4096 -f ~/.ssh/id_rsa -N ""
```

**Shared cluster shortcut:** If the Stencil cluster on orfeo-vm is already provisioned (for example for [Bucket Explorer](https://github.com/luisfpal/s3bucket_manager_app)), skip to [Step 7](#step-7-build-and-push-container-image).

**GitHub PAT:** Create a classic token with `write:packages` scope for pushing to GHCR. Store it in `k8s/.env` — see `k8s/.env.example`.

---

## Steps 1–6: Provision the Stencil Virtual Datacenter

These steps create the KVM VMs, Ceph cluster, FreeIPA, and K3s cluster from scratch. They are shared infrastructure — the same layers used by other AREA Science Park projects on Stencil.

1. Clone the four Stencil provisioning repositories (`tofu-libvirt`, `ceph-provisioning`, `freeipa-provisioning`, `kubernetes-provisioning`).
2. Configure `vars.json` — set `ssh_key_path`, explicit `disk_size_per_vm`, and network addresses.
3. Provision VMs with OpenTofu.
4. Deploy Ceph with Ansible.
5. Deploy FreeIPA with Ansible.
6. Deploy K3s with Ansible; verify nodes are Ready.

**Detailed walkthrough:** follow [Bucket Explorer dev-environment-setup Steps 1–6](https://github.com/luisfpal/s3bucket_manager_app/blob/main/docs/dev-environment-setup.md#step-1-clone-the-stencil-repositories). That document uses TEST-NET-2 addresses (`198.51.100.0/24`); on orfeo-vm use `192.168.132.0/24` instead (K3s nodes at `.10`, `.11`, `.12`).

After Step 6, confirm cluster access:

```bash
kubectl get nodes
# All three nodes should be Ready
```

---

## Step 7: Build and Push Container Image

Images are published to **GitHub Container Registry**:

| Setting | Value |
|---------|-------|
| Image | `ghcr.io/luisfpal/sem-classifier:latest` |
| Push auth | `GHCR_TOKEN` in `k8s/.env` (gitignored) |
| Pull on K3s | Package must be **public** (no imagePullSecrets) |

Create cluster and registry credentials:

```bash
cp k8s/.env.example k8s/.env
# Edit k8s/.env — set GHCR_TOKEN

cat > k8s/env/dev/cluster.local.env <<'ENV'
K3S_API_HOST=192.168.132.10
K3S_SSH_USER=root
K3S_REMOTE_KUBECONFIG=/etc/rancher/k3s/k3s.yaml
K3S_NODES="192.168.132.10 192.168.132.11 192.168.132.12"
ENV
```

Build and push:

```bash
cd k8s
./app.sh build-image
```

After the first push, set the package visibility to **Public** in GitHub Packages settings so K3s nodes can pull without credentials.

This writes a release artifact under `k8s/releases/release-latest.json` pinning the pushed digest.

---

## Step 8: K3s API Tunnel and Port-Forwards

`./app.sh access` opens everything needed for local development:

1. SSH tunnel to the K3s API (`localhost:16443`)
2. Port-forward KrakenD to `localhost:8080`
3. Port-forward Authentik to `localhost:9001` (when the infra namespace exists)

```bash
cd k8s
./app.sh access
```

For manual debugging, the tunnel alone:

```bash
ssh -L 16443:127.0.0.1:6443 root@192.168.132.10
```

---

## Step 9: Deploy This API

### 9.1 Copy local configuration templates

Tracked files under `k8s/env/dev/` are templates. Create gitignored local overrides:

```bash
cp k8s/env/dev/secrets.yaml k8s/env/dev/secrets.local.yaml
cp k8s/env/dev/infra-secrets.yaml k8s/env/dev/infra-secrets.local.yaml
```

Fill real values in `secrets.local.yaml` and `infra-secrets.local.yaml` (Authentik bootstrap password, OIDC client secret, PostgreSQL passwords). See [k8s/env/README.md](../k8s/env/README.md) for file precedence.

**Gateway settings:** The tracked `gateway-settings.json` is prod-shaped (HTTPS placeholders). For the Stencil dev cluster, copy the **workaround example** above — in-cluster HTTP JWKS with `disable_jwk_security: true`. JWT `iss` must match `auth.issuer` in that file; `./app.sh token` uses the same in-cluster Host header. When Authentik gets a public HTTPS hostname, switch to external HTTPS URLs and `disable_jwk_security: false` (same as production).

### 9.2 Deploy identity infra, then the API stack

```bash
cd k8s
./infra.sh deploy
./app.sh deploy --rebuild
./infra.sh configure   # skip if infra was deployed without --skip-config
```

If the API namespace did not exist when infra first ran:

```bash
./infra.sh deploy --skip-config
./app.sh deploy --rebuild
./infra.sh configure
```

### 9.3 Verify

With `./app.sh access` running:

```bash
cd k8s
./app.sh token
curl -s http://localhost:8080/__health

# From repo root:
AUTH_CLIENT_SECRET=<from-secrets.local.yaml> python tests/test_api.py
```

---

## Step 10: Co-Existence with Other Projects

The orfeo-vm Stencil cluster hosts multiple applications in isolated namespaces. This API uses:

| Resource | Value |
|----------|-------|
| App namespace | `sem-classifier` |
| Infra namespace | `authentik-sem-classifier` |
| Local KrakenD port | `8080` |
| Local Authentik port | `9001` |

Check Authentik infra status without touching the API namespace:

```bash
cd k8s
./infra.sh check
./infra.sh status
```

See the [port allocation table](./dev-environment-overview.md#port-allocation-on-the-developer-machine) in the overview doc before starting another project's port-forwards.

---

## Production Path

Development scripts (`app.sh`, `infra.sh`) are **not** used in production. When Authentik is already administered by an infrastructure team, apply only `services/<id>/generated/prod/` with production overlays. See [Production deployment](production-deployment.md).
