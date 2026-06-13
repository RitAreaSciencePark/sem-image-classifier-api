#!/bin/bash
# ==============================================================================
# app.sh - Build, Deploy, and Manage the SEM Image Classifier API (App Layer)
# ==============================================================================
#
# Manages the API stack in sem-classifier (per service) namespace. Authentik is deployed
# separately via infra.sh. Deploy order: ./infra.sh deploy && ./app.sh deploy
#
# USAGE:
#   ./app.sh config               # Show resolved non-secret configuration
#   ./app.sh access               # SSH tunnel + KrakenD/Authentik port-forwards
#   ./app.sh deploy               # Deploy app manifests
#   ./app.sh token                # Get JWT via Authentik client_credentials
#
# This script is calibrated for the Stencil development cluster only.
# Production deploys use manual kubectl apply — see docs/production-deployment.md.
#
# WHAT EACH SUBCOMMAND DOES:
#
#   config:
#     1. Load k8s/env/dev/cluster.env, then optional cluster.local.env.
#     2. Load tracked or local BentoML/gateway/secret config files.
#     3. Print the resolved namespace, image, registry, model, and file choices.
#
#   access:
#     1. Open an SSH tunnel to the configured K3s API host.
#     2. Copy the remote K3s kubeconfig and patch it to use the tunnel.
#     3. Start detached port-forwards for KrakenD and Authentik (dev).
#     4. Verify the local listeners before reporting success.
#
#   build-image:
#     1. Validate the model-source contract from bentoml-config*.yaml.
#     2. Build the container image with the selected BentoML service entrypoint.
#     3. Push the image to GHCR (ghcr.io).
#     4. Write a release artifact that pins the pushed digest.
#
#   deploy:
#     1. Check local tools, SSH access, and kubeconfig.
#     2. Create/update only the configured namespace.
#     3. Apply secrets, BentoML config, Redis, PostgreSQL, KrakenD ConfigMaps,
#        gateway, HPA, and optional dev ingress (05-ingress.dev.yaml).
#     4. Roll deployments to the pinned release digest when available.
#     5. Wait for rollouts and print access instructions.
#
#   bootstrap:
#     1. Build and push first, while the old namespace is still alive.
#     2. Reset only the configured namespace after the image is available.
#     3. Deploy from the pinned release artifact.
#
# CONFIGURATION OWNERSHIP:
#   - Identity lives here: APP_NAME, SERVICE_NAME, NAMESPACE, IMAGE_REPOSITORY,
#     IMAGE_TAG, and BENTOML_SERVICE.
#   - Model/runtime values live in bentoml-config.yaml or an ignored
#     bentoml-config.local.yaml.
#   - Private infrastructure values live in cluster.env plus optional ignored
#     cluster.local.env: K3S_API_HOST, K3S_SSH_USER, K3S_NODES, and
#     K3S_REMOTE_KUBECONFIG.
#   - Container registry (GHCR owner/image) lives in generated deploy.env.
#   - GHCR push credentials live in k8s/.env (gitignored).
#   - cluster.local.env is intentionally not allowed to override identity or
#     model values. This keeps the repository on one public identity.
#
# RESET SAFETY:
#   reset/bootstrap refuse protected namespaces and delete only NAMESPACE. Image
#   build happens before namespace deletion, so a broken model build does not
#   destroy the currently running stack.
#
# PREREQUISITES:
#   - Stencil dev cluster provisioned — see docs/dev-environment-setup.md
#   - SSH access to the configured K3s API host.
#   - podman for image builds.
#   - kubectl for cluster operations.
#   - k8s/.env with GHCR_TOKEN (see k8s/.env.example).
#
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

SERVICE_ID="${SERVICE:-sem-classifier}"
SERVICE_DIR="$PROJECT_DIR/services/$SERVICE_ID"
GENERATED_DIR="$SERVICE_DIR/generated/dev"

# Defaults overwritten by generated/deploy.env after render.
APP_NAME=""
SERVICE_NAME=""
NAMESPACE=""
IMAGE_REPOSITORY=""
IMAGE_TAG="latest"
BENTOML_SERVICE=""
INFRA_NAMESPACE="authentik-reusable-ml-services"
AUTHENTIK_SERVICE="authentik-service"
AUTHENTIK_PF_PORT="${AUTHENTIK_PF_PORT:-9001}"
AUTHENTIK_PF_HTTPS_PORT="${AUTHENTIK_PF_HTTPS_PORT:-9443}"
AUTHENTIK_HOST_FQDN=""
OIDC_APPLICATION_SLUG=""
REGISTRY=""
REGISTRY_SCHEME="https"
IMAGE_NAME=""
PF_PORT="${PF_PORT:-8080}"

[ -f "${SCRIPT_DIR}/.env" ] && source "${SCRIPT_DIR}/.env"

K3S_API_HOST="${K3S_API_HOST:-127.0.0.1}"
K3S_SSH_USER="${K3S_SSH_USER:-root}"
K3S_REMOTE_KUBECONFIG="${K3S_REMOTE_KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"
K3S_NODES="${K3S_NODES:-}"
SECRET_NAME="app-secrets"
TUNNEL_SOCK="/tmp/k3s-api-tunnel.sock"
TUNNEL_KUBECONFIG="/tmp/k3s-tunnel-kubeconfig.yaml"
MANIFESTS_DIR=""
GATEWAY_DIR="$PROJECT_DIR/gateway"
DB_DIR="$PROJECT_DIR/db"
ENV_BASE_DIR="$SCRIPT_DIR/env"
RELEASES_DIR=""

ENV_DIR=""
CLUSTER_CONFIG_FILE=""
CLUSTER_LOCAL_CONFIG_FILE=""
BENTOML_CONFIG_FILE=""
SECRETS_FILE=""
GATEWAY_SETTINGS_FILE=""
RENDERED_GATEWAY_SETTINGS_FILE=""
MODEL_SOURCE=""
MODEL_ID=""
MODEL_REVISION=""
MODEL_CACHE_DIR=""
RESOLVED_MODEL_CACHE_DIR=""
RESOLVED_MODEL_REVISION=""
IMAGE_DIGEST_REF=""
IMAGE_DEPLOY_REF=""
RELEASE_FILE=""

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'
BOLD='\033[1m'

info()    { echo -e "${BLUE}[INFO]${NC} $*" >&2; }
success() { echo -e "${GREEN}[  OK]${NC} $*" >&2; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $*" >&2; }
fail()    { echo -e "${RED}[FAIL]${NC} $*" >&2; exit 1; }
step()    { echo -e "\n${CYAN}${BOLD}━━━ $* ━━━${NC}" >&2; }

validate_cluster_env_file() {
    local file="$1"
    local invalid_keys
    invalid_keys="$(
        sed -nE 's/^[[:space:]]*(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)=.*/\2/p' "$file" \
            | grep -Ev '^(K3S_API_HOST|K3S_SSH_USER|K3S_REMOTE_KUBECONFIG|K3S_NODES)$' \
            || true
    )"
    if [ -n "$invalid_keys" ]; then
        fail "$file may contain only private infrastructure keys. Move identity to app.sh and model values to bentoml-config*.yaml. Invalid keys: $(echo "$invalid_keys" | paste -sd ', ' -)"
    fi
}

source_cluster_env_file() {
    local file="$1"
    local label="$2"
    [ -f "$file" ] || return 0
    validate_cluster_env_file "$file"
    # shellcheck disable=SC1090
    source "$file"
    info "Loaded ${label}: $file"
}

ghcr_login() {
    if [ -z "${GHCR_TOKEN:-}" ]; then
        fail "GHCR_TOKEN is not set. Add it to k8s/.env — see k8s/.env.example."
    fi
    info "Logging in to ghcr.io as ${GHCR_OWNER}..."
    echo "${GHCR_TOKEN}" | podman login ghcr.io -u "${GHCR_OWNER}" --password-stdin
    success "Authenticated to ghcr.io"
}

load_service_context() {
    local deploy_env="$GENERATED_DIR/deploy.env"
    [ -f "$deploy_env" ] || fail "Missing $deploy_env — run: make render SERVICE=$SERVICE_ID"
    # shellcheck disable=SC1090
    source "$deploy_env"

    MANIFESTS_DIR="$GENERATED_DIR/k8s"
    RELEASES_DIR="$SERVICE_DIR/releases"
    BENTOML_CONFIG_FILE="$GENERATED_DIR/bentoml-config.yaml"
    SECRETS_FILE="$SERVICE_DIR/secrets.local.yaml"
    [ -f "$SECRETS_FILE" ] || SECRETS_FILE="$GENERATED_DIR/secrets.yaml.example"

    GATEWAY_SETTINGS_FILE="$GENERATED_DIR/gateway-settings.json"
    if [ -f "$SERVICE_DIR/gateway-settings.local.json" ]; then
        GATEWAY_SETTINGS_FILE="$SERVICE_DIR/gateway-settings.local.json"
    elif [ -f "$GENERATED_DIR/gateway-settings.dev-workaround.json" ]; then
        GATEWAY_SETTINGS_FILE="$GENERATED_DIR/gateway-settings.dev-workaround.json"
    fi

    [ -n "${REGISTRY:-}" ] || fail "REGISTRY missing from deploy.env — run: make render SERVICE=$SERVICE_ID"
    [ -n "${IMAGE_NAME:-}" ] || fail "IMAGE_NAME missing from deploy.env — run: make render SERVICE=$SERVICE_ID"
    [ -n "${GHCR_OWNER:-}" ] || fail "GHCR_OWNER missing from deploy.env — run: make render SERVICE=$SERVICE_ID"
    AUTHENTIK_ISSUER_HOST="${AUTHENTIK_HOST_FQDN}:9443"
    PF_PORT="${PF_PORT:-8080}"
    AUTHENTIK_PF_PORT="${AUTHENTIK_PF_PORT:-9001}"
    AUTHENTIK_PF_HTTPS_PORT="${AUTHENTIK_PF_HTTPS_PORT:-9443}"
}

parse_cli_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --service)
                SERVICE_ID="${2:-}"
                SERVICE_DIR="$PROJECT_DIR/services/$SERVICE_ID"
                GENERATED_DIR="$SERVICE_DIR/generated/dev"
                shift 2
                ;;
            *)
                break
                ;;
        esac
    done
    printf '%s\n' "$@"
}


usage() {
    echo -e "${BOLD}app.sh - SEM Image Classifier API (App Layer)${NC}"
    echo ""
    echo "  Usage: ./app.sh <command> [args]"
    echo ""
    echo "  Commands:"
    echo "    config"
    echo "    deploy [--rebuild] [--use-release]"
    echo "    build-image"
    echo "    build"
    echo "    bootstrap"
    echo "    status"
    echo "    access"
    echo "    logs [component]"
    echo "    restart <component>"
    echo "    token [username]"
    echo "    reset [--yes]"
    echo "    cleanup"
    echo ""
    echo "  Defaults:"
    echo "    APP_NAME=${APP_NAME}"
    echo "    SERVICE_NAME=${SERVICE_NAME}"
    echo "    NAMESPACE=${NAMESPACE}"
    echo "    IMAGE_NAME=${IMAGE_NAME}"
    echo "    BENTOML_SERVICE=${BENTOML_SERVICE}"
}

assert_safe_namespace() {
    # Namespace deletion is the only destructive operation in this script.
    # Keep the guard close to every command path that can touch Kubernetes.
    [ -n "$NAMESPACE" ] || fail "NAMESPACE cannot be empty"
    case "$NAMESPACE" in
        default|kube-system|kube-public|kube-node-lease|storage-system)
            fail "Refusing to operate on protected namespace: $NAMESPACE"
            ;;
    esac
    if ! [[ "$NAMESPACE" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]]; then
        fail "NAMESPACE must be a valid Kubernetes DNS label: $NAMESPACE"
    fi
}

ensure_kubeconfig() {
    # All kubectl calls use the tunnel kubeconfig unless the operator has
    # explicitly exported KUBECONFIG. This keeps local setup repeatable.
    export KUBECONFIG="${KUBECONFIG:-$TUNNEL_KUBECONFIG}"
    if [ ! -f "$KUBECONFIG" ]; then
        fail "Kubeconfig not found: $KUBECONFIG\n  Run: ./app.sh access"
    fi
    if ! kubectl cluster-info &>/dev/null 2>&1; then
        fail "Cannot connect to K3s. SSH tunnel may be down.\n  Run: ./app.sh access"
    fi
}

ensure_namespace() {
    assert_safe_namespace
    kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -
    success "Namespace ready: $NAMESPACE"
}

stop_infra_port_forward_on_port() {
    local local_port="$1"
    local pids
    pids="$(pgrep -f "kubectl port-forward svc/${AUTHENTIK_SERVICE} ${local_port}:.* -n ${INFRA_NAMESPACE}" || true)"
    [ -n "$pids" ] || return 0
    while read -r pid; do
        [ -n "$pid" ] || continue
        kill "$pid" 2>/dev/null || true
    done <<< "$pids"
}

reclaim_legacy_authentik_port_9000() {
    # sem-classifier uses :9001 Authentik PF so s3bucket can own :9000.
    # Kill stale forwards from older app.sh versions that still bind localhost:9000.
    [ "$AUTHENTIK_PF_PORT" = "9000" ] && return 0
    local pids
    pids="$(pgrep -f "kubectl port-forward svc/${AUTHENTIK_SERVICE} 9000:.* -n ${INFRA_NAMESPACE}" || true)"
    [ -n "$pids" ] || return 0
    warn "Stopping legacy Authentik port-forward on :9000 (this project uses :${AUTHENTIK_PF_PORT}; :9000 is for s3bucket)"
    while read -r pid; do
        [ -n "$pid" ] || continue
        kill "$pid" 2>/dev/null || true
    done <<< "$pids"
    stop_local_port_forward "9000"
}

start_infra_port_forward() {
    local service="$1"
    local local_port="$2"
    local remote_port="$3"
    local log_file="$4"

    stop_infra_port_forward_on_port "$local_port"
    stop_local_port_forward "$local_port"
    : > "$log_file"
    setsid kubectl port-forward "svc/${service}" "${local_port}:${remote_port}" -n "$INFRA_NAMESPACE" >"$log_file" 2>&1 &
    local pid="$!"
    for _ in {1..20}; do
        if ss -tln 2>/dev/null | grep -q ":${local_port} "; then
            return 0
        fi
        if ! kill -0 "$pid" 2>/dev/null; then
            fail "Port-forward for svc/${service} (ns=$INFRA_NAMESPACE) failed:\n$(tail -20 "$log_file")"
        fi
        sleep 0.25
    done
    fail "Port-forward for svc/${service} did not open localhost:${local_port}:\n$(tail -20 "$log_file")"
}

warn_if_authentik_missing() {
    if ! kubectl get svc/"$AUTHENTIK_SERVICE" -n "$INFRA_NAMESPACE" &>/dev/null 2>&1; then
        warn "Authentik service not found in $INFRA_NAMESPACE"
        warn "Deploy identity infra first: ./infra.sh deploy"
    fi
}

stop_service_port_forward() {
    # Namespace resets invalidate old pod-backed port-forwards. Kill only this
    # stack's forwards, not unrelated forwards an operator may be using.
    local service="$1"
    local pids
    pids="$(pgrep -f "kubectl port-forward svc/${service} .* -n ${NAMESPACE}" || true)"
    [ -n "$pids" ] || return 0
    while read -r pid; do
        [ -n "$pid" ] || continue
        kill "$pid" 2>/dev/null || true
    done <<< "$pids"
}

stop_local_port_forward() {
    # A namespace reset can leave kubectl listening on the right local port while
    # forwarding to a dead pod. The service/name grep above misses old namespace
    # forwards, so also clean kubectl listeners on the exact ports this script owns.
    local local_port="$1"
    local pids
    pids="$(ss -tlnp 2>/dev/null | awk -v port=":${local_port}" '$4 ~ port { print }' | grep 'kubectl' | grep -oP 'pid=\K[0-9]+' || true)"
    [ -n "$pids" ] || return 0
    while read -r pid; do
        [ -n "$pid" ] || continue
        kill "$pid" 2>/dev/null || true
    done <<< "$pids"
}

start_service_port_forward() {
    # Detached port-forwards survive after app.sh exits. We verify the listener
    # before returning because a launched process is not proof of usable access.
    local service="$1"
    local local_port="$2"
    local remote_port="$3"
    local log_file="$4"

    stop_service_port_forward "$service"
    stop_local_port_forward "$local_port"
    : > "$log_file"
    setsid kubectl port-forward "svc/${service}" "${local_port}:${remote_port}" -n "$NAMESPACE" >"$log_file" 2>&1 &
    local pid="$!"
    for _ in {1..20}; do
        if ss -tln 2>/dev/null | grep -q ":${local_port} "; then
            return 0
        fi
        if ! kill -0 "$pid" 2>/dev/null; then
            fail "Port-forward for svc/${service} failed:\n$(tail -20 "$log_file")"
        fi
        sleep 0.25
    done
    fail "Port-forward for svc/${service} did not open localhost:${local_port}:\n$(tail -20 "$log_file")"
}

read_yaml_value() {
    local file="$1"
    local key="$2"
    local line
    line="$(grep -E "^[[:space:]]*${key}:" "$file" | head -n 1 || true)"
    [ -n "$line" ] || return 0
    printf '%s\n' "$line" | sed -E 's/^[^:]+:[[:space:]]*//; s/^"//; s/"$//'
}

resolve_dev_environment_files() {
    load_service_context

    ENV_DIR="$ENV_BASE_DIR/dev"
    CLUSTER_CONFIG_FILE="$ENV_DIR/cluster.env"
    CLUSTER_LOCAL_CONFIG_FILE="$ENV_DIR/cluster.local.env"

    source_cluster_env_file "$CLUSTER_CONFIG_FILE" "tracked cluster template"
    source_cluster_env_file "$CLUSTER_LOCAL_CONFIG_FILE" "local infrastructure overrides"

    [ -d "$MANIFESTS_DIR" ] || fail "Missing generated manifests: $MANIFESTS_DIR"
    [ -f "$BENTOML_CONFIG_FILE" ] || fail "Missing bentoml config: $BENTOML_CONFIG_FILE"
    [ -f "$SECRETS_FILE" ] || fail "Missing secrets file: $SECRETS_FILE (copy from generated/secrets.yaml.example)"
    [ -f "$GATEWAY_SETTINGS_FILE" ] || fail "Missing gateway settings: $GATEWAY_SETTINGS_FILE"

    if printenv MODEL_ID >/dev/null 2>&1 || printenv MODEL_REVISION >/dev/null 2>&1 || printenv MODEL_SOURCE >/dev/null 2>&1 || printenv MODEL_CACHE_DIR >/dev/null 2>&1; then
        fail "MODEL_* shell overrides are blocked. Edit $BENTOML_CONFIG_FILE instead."
    fi

    MODEL_SOURCE="$(read_yaml_value "$BENTOML_CONFIG_FILE" MODEL_SOURCE)"
    MODEL_ID="$(read_yaml_value "$BENTOML_CONFIG_FILE" MODEL_ID)"
    MODEL_REVISION="$(read_yaml_value "$BENTOML_CONFIG_FILE" MODEL_REVISION)"
    MODEL_CACHE_DIR="$(read_yaml_value "$BENTOML_CONFIG_FILE" MODEL_CACHE_DIR)"

    [ -n "$MODEL_SOURCE" ] || MODEL_SOURCE="hugging_face"
    case "$MODEL_SOURCE" in
        hf_public) MODEL_SOURCE="hugging_face" ;;
        local_dir|private_cache) MODEL_SOURCE="private" ;;
    esac
    case "$MODEL_SOURCE" in
        hugging_face|private) ;;
        *) fail "MODEL_SOURCE must be hugging_face or private in $BENTOML_CONFIG_FILE" ;;
    esac

    RESOLVED_MODEL_CACHE_DIR=""
    RESOLVED_MODEL_REVISION="$MODEL_REVISION"

    if [ "$MODEL_SOURCE" = "hugging_face" ]; then
        [ -n "$MODEL_ID" ] || fail "MODEL_ID is required when MODEL_SOURCE=hugging_face"
        [ -n "$MODEL_REVISION" ] || fail "MODEL_REVISION is required when MODEL_SOURCE=hugging_face"
    else
        [ -n "$MODEL_ID" ] || fail "MODEL_ID is required when MODEL_SOURCE=private"
        [ -n "$MODEL_CACHE_DIR" ] || fail "MODEL_CACHE_DIR is required when MODEL_SOURCE=private"
        [[ "$MODEL_CACHE_DIR" = /* ]] || fail "MODEL_CACHE_DIR must be absolute (got: $MODEL_CACHE_DIR)"
        RESOLVED_MODEL_CACHE_DIR="$(realpath -e "$MODEL_CACHE_DIR" 2>/dev/null || true)"
        [ -n "$RESOLVED_MODEL_CACHE_DIR" ] || fail "MODEL_CACHE_DIR does not exist: $MODEL_CACHE_DIR"
        [ -d "$RESOLVED_MODEL_CACHE_DIR" ] || fail "MODEL_CACHE_DIR must be a directory: $RESOLVED_MODEL_CACHE_DIR"
    fi
}

write_release_artifact() {
    # Release artifacts pin the pushed digest that deployment should use. This
    # prevents namespace resets from racing ahead of a successful image build.
    mkdir -p "$RELEASES_DIR"
    local ts
    ts="$(date -u +"%Y%m%dT%H%M%SZ")"
    RELEASE_FILE="$RELEASES_DIR/release-${ts}.json"

    cat > "$RELEASE_FILE" <<JSON
{
  "created_at_utc": "$ts",
  "namespace": "$NAMESPACE",
  "image_name": "$IMAGE_NAME",
  "image_deploy_ref": "$IMAGE_DEPLOY_REF",
  "image_digest_ref": "$IMAGE_DIGEST_REF",
  "model_source": "$MODEL_SOURCE",
  "model_id": "$MODEL_ID",
  "model_revision": "$RESOLVED_MODEL_REVISION",
  "model_cache_dir": "$RESOLVED_MODEL_CACHE_DIR"
}
JSON

    ln -sfn "$(basename "$RELEASE_FILE")" "$RELEASES_DIR/release-latest.json"
    success "Release artifact written: $RELEASE_FILE"
}

load_latest_release_artifact() {
    local latest_file="$RELEASES_DIR/release-latest.json"
    [ -f "$latest_file" ] || fail "Missing release artifact: $latest_file (run ./app.sh build-image first)"

    IMAGE_DEPLOY_REF="$(python3 - <<'PY' "$latest_file"
import json, sys
with open(sys.argv[1], 'r', encoding='utf-8') as fh:
    data = json.load(fh)
print(data.get('image_deploy_ref') or data.get('image_digest_ref') or data.get('image_name') or '')
PY
)"
    [ -n "$IMAGE_DEPLOY_REF" ] || fail "Invalid release artifact: missing image ref in $latest_file"
    case "$IMAGE_DEPLOY_REF" in
        *"/${IMAGE_REPOSITORY}:"*|*"/${IMAGE_REPOSITORY}@"*) ;;
        *) fail "Release artifact image does not match IMAGE_REPOSITORY=$IMAGE_REPOSITORY: $IMAGE_DEPLOY_REF" ;;
    esac
    info "Using release artifact: $latest_file"
}



build_and_distribute() {
    # Build validates the selected model before anything in the namespace is
    # deleted. If model loading fails, deployment stops while the old stack lives.
    local start_time
    start_time="$(date +%s)"
    local model_cache_context="/tmp/${APP_NAME}-empty-model-cache"
    mkdir -p "$model_cache_context"
    if [ "$MODEL_SOURCE" = "private" ]; then
        model_cache_context="$RESOLVED_MODEL_CACHE_DIR"
    fi

    step "Building container image"
    info "Model build args: MODEL_SOURCE=$MODEL_SOURCE MODEL_ID=$MODEL_ID MODEL_REVISION=${RESOLVED_MODEL_REVISION:-<empty>} MODEL_CACHE_DIR=${RESOLVED_MODEL_CACHE_DIR:-<none>}"
    podman build \
        --build-context model_cache="$model_cache_context" \
        --build-arg MODEL_SOURCE="$MODEL_SOURCE" \
        --build-arg MODEL_ID="$MODEL_ID" \
        --build-arg MODEL_REVISION="$RESOLVED_MODEL_REVISION" \
        --build-arg BENTOML_SERVICE="$BENTOML_SERVICE" \
        -t "$IMAGE_NAME" \
        -f "$PROJECT_DIR/Containerfile" \
        "$PROJECT_DIR"
    success "Image built"

    step "Pushing image to GHCR (${REGISTRY})"
    ghcr_login
    local push_digest_file
    push_digest_file="$(mktemp)"
    podman push --digestfile "$push_digest_file" "$IMAGE_NAME"
    success "Image pushed to ${REGISTRY}"

    local pushed_digest
    pushed_digest="$(tr -d '[:space:]' < "$push_digest_file")"
    rm -f "$push_digest_file"
    if [[ "$pushed_digest" =~ ^sha256:[0-9a-f]{64}$ ]]; then
        IMAGE_DIGEST_REF="${IMAGE_NAME%:*}@${pushed_digest}"
    else
        IMAGE_DIGEST_REF="$(podman image inspect "$IMAGE_NAME" --format '{{index .RepoDigests 0}}' 2>/dev/null || true)"
    fi
    [ -n "$IMAGE_DIGEST_REF" ] || fail "Could not resolve pushed image digest for $IMAGE_NAME"
    IMAGE_DEPLOY_REF="$IMAGE_DIGEST_REF"
    success "Image digest: $IMAGE_DIGEST_REF"


    write_release_artifact
    success "Image built and pushed in $(( $(date +%s) - start_time ))s"
}

generate_gateway_configmaps() {
    # KrakenD flexible configuration reads templates/settings/plugins from
    # ConfigMaps. Regenerate them from source files on each deploy/restart.
    step "Generating KrakenD ConfigMaps"
    "$SCRIPT_DIR/render-gateway-configmaps.sh" \
        --namespace "$NAMESPACE" \
        --gateway-settings "$GATEWAY_SETTINGS_FILE" \
        --service-name "$SERVICE_NAME"
    success "KrakenD ConfigMaps applied"
}

apply_manifest() {
    local manifest="$1"
    kubectl apply -n "$NAMESPACE" -f "$manifest" && success "Applied $(basename "$manifest")"
}

rollout_with_retry() {
    local resource="$1"
    local timeout="${2:-120s}"
    local attempts="${3:-2}"
    local attempt

    for ((attempt=1; attempt<=attempts; attempt++)); do
        if kubectl rollout status "$resource" -n "$NAMESPACE" --timeout="$timeout"; then
            return 0
        fi
        warn "$resource rollout attempt ${attempt}/${attempts} failed (ns=$NAMESPACE)"
        [ "$attempt" -lt "$attempts" ] && info "Retrying rollout wait for $resource..."
    done
    fail "$resource rollout did not complete after ${attempts} attempts (ns=$NAMESPACE)"
}

wait_for_rollout() {
    local resource="$1"
    local timeout="${2:-300s}"
    local attempts="${3:-2}"
    info "Waiting for $resource..."
    rollout_with_retry "$resource" "$timeout" "$attempts"
    success "$resource is ready"
}

apply_dev_ingress_if_available() {
    local dev_ingress_file="$MANIFESTS_DIR/05-ingress.dev.yaml"
    [ -f "$dev_ingress_file" ] || return 0

    local ingress_class
    ingress_class=$(grep 'ingressClassName:' "$dev_ingress_file" 2>/dev/null | awk '{print $2}')
    if [ -n "$ingress_class" ] && kubectl get ingressclass "$ingress_class" &>/dev/null 2>&1; then
        kubectl apply -f "$dev_ingress_file"
        success "Dev Ingress applied (catch-all via ${ingress_class})"
    else
        info "Dev Ingress skipped — IngressClass '${ingress_class:-unknown}' not in this cluster"
        info "  Use './app.sh access' (direct port-forward) to reach the API"
    fi
}

apply_app_manifests() {
    step "Deploying API stack to $NAMESPACE"

    ensure_namespace

    # Remove legacy NetworkPolicies if a previous dev deploy applied them.
    kubectl delete networkpolicy --all -n "$NAMESPACE" --ignore-not-found >/dev/null 2>&1 || true

    kubectl apply -n "$NAMESPACE" -f "$SECRETS_FILE" && success "Applied $(basename "$SECRETS_FILE")"
    kubectl apply -n "$NAMESPACE" -f "$BENTOML_CONFIG_FILE" && success "Applied $(basename "$BENTOML_CONFIG_FILE")"

    generate_gateway_configmaps

    apply_manifest "$MANIFESTS_DIR/01-redis.yaml"
    wait_for_rollout "statefulset/redis" "120s" "2"

    apply_manifest "$MANIFESTS_DIR/03-postgresql.yaml"
    wait_for_rollout "statefulset/postgresql" "120s" "2"

    apply_manifest "$MANIFESTS_DIR/04-krakend.yaml"
    wait_for_rollout "deployment/krakend" "300s" "2"

    apply_manifest "$MANIFESTS_DIR/02-bentoml.yaml"
    if [ -n "$IMAGE_DEPLOY_REF" ]; then
        info "Setting BentoML deployment image to $IMAGE_DEPLOY_REF"
        kubectl set image deployment/bentoml bentoml="$IMAGE_DEPLOY_REF" -n "$NAMESPACE" >/dev/null
    fi
    if [ "${REBUILD_BENTOML:-false}" = true ]; then
        info "Restarting BentoML deployment to pull rebuilt :latest image"
        kubectl rollout restart deployment/bentoml -n "$NAMESPACE" >/dev/null
    fi
    wait_for_rollout "deployment/bentoml" "600s" "2"

    apply_manifest "$MANIFESTS_DIR/07-hpa.yaml"
    apply_dev_ingress_if_available

    success "App manifests applied"
}

check_cluster_nodes() {
    [ -n "$K3S_NODES" ] || { info "K3S_NODES not configured; skipping node SSH diagnostics"; return 0; }
    step "K3s node diagnostics"
    local node
    for node in $K3S_NODES; do
        if ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no -o BatchMode=yes \
            "${K3S_SSH_USER}@${node}" "echo ok" >/dev/null 2>&1; then
            success "SSH reachable: ${node}"
        else
            warn "SSH not reachable: ${node}"
        fi
    done
}

show_config() {
    resolve_dev_environment_files

    step "Resolved configuration"
    cat <<EOF
Environment:              dev (hardcoded)
App name:                 $APP_NAME
Service name:             $SERVICE_NAME
Namespace:                $NAMESPACE
BentoML service:          $BENTOML_SERVICE
Image:                    $IMAGE_NAME
Registry endpoint:        ${REGISTRY_SCHEME}://${REGISTRY}
K3s API host:             ${K3S_SSH_USER}@${K3S_API_HOST}
K3s remote kubeconfig:    $K3S_REMOTE_KUBECONFIG
K3s nodes:                ${K3S_NODES:-<not configured>}
BentoML config file:      $BENTOML_CONFIG_FILE
Gateway settings file:    $GATEWAY_SETTINGS_FILE
Secrets file:             $SECRETS_FILE
Model source:             $MODEL_SOURCE
Model ID:                 $MODEL_ID
Model revision:           ${RESOLVED_MODEL_REVISION:-<empty>}
Model cache directory:    ${RESOLVED_MODEL_CACHE_DIR:-<none>}
EOF
}

deploy() {
    # Deployment order follows runtime dependencies:
    # Redis/PostgreSQL first, KrakenD next, BentoML last.
    local rebuild=false
    local use_release=false
    REBUILD_BENTOML=false
    while [ $# -gt 0 ]; do
        case "$1" in
            --rebuild) rebuild=true ;;
            --use-release) use_release=true ;;
            *) fail "Unknown deploy argument: $1" ;;
        esac
        shift
    done

    resolve_dev_environment_files
    assert_safe_namespace

    step "Pre-flight checks"
    command -v podman &>/dev/null || fail "podman not found"
    command -v kubectl &>/dev/null || fail "kubectl not found"
    ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no -o BatchMode=yes \
        "${K3S_SSH_USER}@${K3S_API_HOST}" "echo ok" &>/dev/null 2>&1 || fail "Cannot SSH to ${K3S_SSH_USER}@${K3S_API_HOST}"
    ensure_kubeconfig
    success "Prerequisites OK"
    check_cluster_nodes

    step "Container image"
    if [ "$use_release" = true ]; then
        load_latest_release_artifact
    elif [ "$rebuild" = true ]; then
        build_and_distribute
        REBUILD_BENTOML=true
    elif [ -f "$RELEASES_DIR/release-latest.json" ]; then
        load_latest_release_artifact
    else
        warn "No release artifact found; deploying mutable tag $IMAGE_NAME"
        warn "Run ./app.sh deploy --rebuild to build and push a fresh image to GHCR"
        IMAGE_DEPLOY_REF="$IMAGE_DIGEST_REF"
    fi

    warn_if_authentik_missing
    apply_app_manifests

    success "Deployment complete"
    kubectl get pods -n "$NAMESPACE" -o wide
    print_access_instructions
}

build_image() {
    resolve_dev_environment_files
    build_and_distribute
}

build() {
    build_image "$@"
    ensure_kubeconfig
    kubectl rollout restart deployment/bentoml -n "$NAMESPACE"
    kubectl rollout status deployment/bentoml -n "$NAMESPACE" --timeout=600s
}

reset_namespace() {
    # Destructive by design, but scoped to the configured namespace and blocked
    # for protected cluster namespaces.
    local auto_yes="${1:-}"
    assert_safe_namespace
    ensure_kubeconfig

    step "Reset namespace"
    warn "This deletes ONLY namespace '$NAMESPACE'"
    if [ "$auto_yes" != "--yes" ]; then
        read -p "Continue? (y/N) " -r
        [[ "$REPLY" =~ ^[Yy]$ ]] || { info "Aborted"; return; }
    fi

    stop_local_port_forward "$PF_PORT"
    stop_local_port_forward "$AUTHENTIK_PF_PORT"
    stop_local_port_forward "$AUTHENTIK_PF_HTTPS_PORT"

    kubectl delete namespace "$NAMESPACE" --ignore-not-found=true --timeout=120s
    success "Namespace reset request completed: $NAMESPACE"
}

bootstrap() {
    # Safe reset flow: build and push image, write release artifact, then reset
    # namespace and deploy from that immutable image reference.
    resolve_dev_environment_files
    ensure_kubeconfig
    build_and_distribute
    reset_namespace --yes
    deploy --use-release
}

show_status() {
    assert_safe_namespace
    ensure_kubeconfig
    step "Pod Status"
    kubectl get pods -n "$NAMESPACE" -o wide || true
    step "Port-Forwards"
    ss -tlnp 2>/dev/null | grep ":${PF_PORT} " >/dev/null && success "KrakenD :${PF_PORT} active" || warn "KrakenD :${PF_PORT} not running"
    step "SSH Tunnel"
    [ -S "$TUNNEL_SOCK" ] && success "K3s API tunnel active" || warn "K3s API tunnel not running"
}

setup_access() {
    resolve_dev_environment_files
    assert_safe_namespace

    step "Setting up access"
    if [ -S "$TUNNEL_SOCK" ] && ! ssh -S "$TUNNEL_SOCK" -O check "${K3S_SSH_USER}@${K3S_API_HOST}" &>/dev/null 2>&1; then
        rm -f "$TUNNEL_SOCK"
    fi
    if [ ! -S "$TUNNEL_SOCK" ]; then
        ssh -fNM -S "$TUNNEL_SOCK" \
            -L 16443:127.0.0.1:6443 \
            -o StrictHostKeyChecking=no \
            -o ExitOnForwardFailure=yes \
            "${K3S_SSH_USER}@${K3S_API_HOST}"
    fi
    success "SSH tunnel ready"

    scp -o StrictHostKeyChecking=no "${K3S_SSH_USER}@${K3S_API_HOST}:${K3S_REMOTE_KUBECONFIG}" "$TUNNEL_KUBECONFIG" &>/dev/null
    sed -i 's|server: https://127.0.0.1:6443|server: https://127.0.0.1:16443|' "$TUNNEL_KUBECONFIG"
    export KUBECONFIG="$TUNNEL_KUBECONFIG"
    check_cluster_nodes

    reclaim_legacy_authentik_port_9000

    start_service_port_forward "krakend" "$PF_PORT" "8080" "/tmp/pf-krakend-${SERVICE_ID}.log"

    if kubectl get svc/"$AUTHENTIK_SERVICE" -n "$INFRA_NAMESPACE" &>/dev/null 2>&1; then
        start_infra_port_forward "$AUTHENTIK_SERVICE" "$AUTHENTIK_PF_PORT" "9000" "/tmp/pf-authentik.log"
        start_infra_port_forward "$AUTHENTIK_SERVICE" "$AUTHENTIK_PF_HTTPS_PORT" "9443" "/tmp/pf-authentik-https.log"
    fi
    success "Access ready"
    print_access_instructions
}

print_access_instructions() {
    echo ""
    echo -e "  ${BOLD}Local tunnel:${NC} ssh -L ${PF_PORT}:localhost:${PF_PORT} -L ${AUTHENTIK_PF_PORT}:localhost:${AUTHENTIK_PF_PORT} ${K3S_SSH_USER}@${K3S_API_HOST}"
    echo -e "  ${BOLD}Health:${NC} curl http://localhost:${PF_PORT}/__health"
    echo -e "  ${BOLD}Token:${NC} cd k8s && ./app.sh token"
    echo -e "  ${BOLD}Submit:${NC} POST http://localhost:${PF_PORT}/api/v1/inference"
    echo ""
}

show_logs() {
    local component="${1:-bentoml}"
    assert_safe_namespace
    ensure_kubeconfig
    case "$component" in
        bentoml|redis|krakend|postgresql)
            kubectl logs -l "app.kubernetes.io/name=$component" -n "$NAMESPACE" --tail=50 -f
            ;;
        postgres)
            kubectl logs -l app.kubernetes.io/name=postgresql -n "$NAMESPACE" --tail=50 -f
            ;;
        krakend-init)
            local pod
            pod="$(kubectl get pods -l app.kubernetes.io/name=krakend -n "$NAMESPACE" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)"
            [ -n "$pod" ] && kubectl logs "$pod" -c plugin-builder -n "$NAMESPACE" || warn "No krakend pod found"
            ;;
        *) fail "Unknown component: $component" ;;
    esac
}

restart_component() {
    local component="$1"
    shift || true
    resolve_dev_environment_files
    assert_safe_namespace
    ensure_kubeconfig
    case "$component" in
        krakend)
            generate_gateway_configmaps
            kubectl rollout restart deployment/krakend -n "$NAMESPACE"
            kubectl rollout status deployment/krakend -n "$NAMESPACE" --timeout=300s
            ;;
        bentoml)
            kubectl rollout restart deployment/bentoml -n "$NAMESPACE"
            kubectl rollout status deployment/bentoml -n "$NAMESPACE" --timeout=600s
            ;;
        redis)
            kubectl rollout restart statefulset/redis -n "$NAMESPACE"
            kubectl rollout status statefulset/redis -n "$NAMESPACE" --timeout=120s
            ;;
        postgresql|postgres)
            kubectl rollout restart statefulset/postgresql -n "$NAMESPACE"
            kubectl rollout status statefulset/postgresql -n "$NAMESPACE" --timeout=120s
            ;;
        *) fail "Unknown component: $component" ;;
    esac
    success "$component restarted"
}

get_token() {
    assert_safe_namespace
    ensure_kubeconfig
    resolve_dev_environment_files

    if ! ss -tlnp 2>/dev/null | grep -q ":${AUTHENTIK_PF_PORT} "; then
        start_infra_port_forward "$AUTHENTIK_SERVICE" "$AUTHENTIK_PF_PORT" "9000" "/tmp/pf-authentik.log"
    fi

    local client_id client_secret token_url
    client_id="$(kubectl get secret app-secrets -n "$NAMESPACE" -o jsonpath='{.data.oidc-client-id}' | base64 -d)"
    client_secret="$(kubectl get secret app-secrets -n "$NAMESPACE" -o jsonpath='{.data.oidc-client-secret}' | base64 -d)"
    [ -n "$client_id" ] || fail "oidc-client-id missing from app-secrets"
    [ -n "$client_secret" ] || fail "oidc-client-secret missing from app-secrets"

    token_url="http://localhost:${AUTHENTIK_PF_PORT}/application/o/token/"
    local response
    response="$(curl -s -X POST "$token_url" \
        -H "Host: ${AUTHENTIK_HOST_FQDN}:9000" \
        -H "Content-Type: application/x-www-form-urlencoded" \
        -d "grant_type=client_credentials&scope=openid profile&client_id=${client_id}&client_secret=${client_secret}" 2>/dev/null)"
    local token
    token="$(echo "$response" | python3 -c "import sys,json; print(json.load(sys.stdin).get('access_token',''))" 2>/dev/null)"
    [ -n "$token" ] || fail "Failed to get token from Authentik. Response: $response"
    echo "$token"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --service)
            SERVICE_ID="${2:-}"
            SERVICE_DIR="$PROJECT_DIR/services/$SERVICE_ID"
            GENERATED_DIR="$SERVICE_DIR/generated/dev"
            shift 2
            ;;
        *)
            break
            ;;
    esac
done

COMMAND="${1:-}"
[ -n "$COMMAND" ] || { usage; exit 1; }
shift || true

case "$COMMAND" in
    config) show_config "$@" ;;
    deploy) deploy "$@" ;;
    build-image) build_image "$@" ;;
    build) build "$@" ;;
    bootstrap) bootstrap "$@" ;;
    status) show_status ;;
    access) setup_access "$@" ;;
    logs) show_logs "${1:-bentoml}" ;;
    restart) [ -n "${1:-}" ] || fail "Usage: ./app.sh restart <component>"; restart_component "$@" ;;
    token) get_token ;;
    reset) resolve_dev_environment_files; [ "${1:-}" = "--yes" ] && reset_namespace --yes || reset_namespace ;;
    cleanup) resolve_dev_environment_files; reset_namespace ;;
    *) usage; exit 1 ;;
esac
