#!/bin/bash
# ==============================================================================
# dev.sh - Build, Deploy, and Manage the SEM Image Classifier on K3s
# ==============================================================================
#
# USAGE:
#   ./dev.sh config --env dev     # Show resolved non-secret configuration
#   ./dev.sh access --env dev     # Set up SSH tunnel + local port-forwards
#   ./dev.sh build-image --env dev # Build image, push it, write release artifact
#   ./dev.sh deploy --env dev     # Deploy manifests, building image if needed
#   ./dev.sh bootstrap --env dev  # Build image, reset namespace, deploy digest
#   ./dev.sh build --env dev      # Build image + restart BentoML deployment
#   ./dev.sh status               # Show pods, local forwards, tunnel status
#   ./dev.sh logs [component]     # Tail logs: bentoml, redis, krakend, postgresql
#   ./dev.sh restart <component>  # Restart one component without rebuilding
#   ./dev.sh token [username]     # Get a dev JWT from mock-oidc
#   ./dev.sh reset [--yes]        # Delete only the configured namespace
#   ./dev.sh cleanup              # Interactive alias for reset
#
# WHAT EACH SUBCOMMAND DOES:
#
#   config:
#     1. Load k8s/env/<env>/cluster.env, then optional cluster.local.env.
#     2. Load tracked or local BentoML/gateway/secret config files.
#     3. Print the resolved namespace, image, registry, model, and file choices.
#
#   access:
#     1. Open an SSH tunnel to the configured K3s API host.
#     2. Copy the remote K3s kubeconfig and patch it to use the tunnel.
#     3. Start detached port-forwards for KrakenD and dev mock-oidc.
#     4. Verify the local listeners before reporting success.
#
#   build-image:
#     1. Validate the model-source contract from bentoml-config*.yaml.
#     2. Build the container image with the selected BentoML service entrypoint.
#     3. Push the image to the configured registry.
#     4. Write a release artifact that pins the pushed digest.
#
#   deploy:
#     1. Check local tools, SSH access, kubeconfig, and registry image state.
#     2. Create/update only the configured namespace.
#     3. Apply secrets, BentoML config, Redis, BentoML, PostgreSQL, mock-oidc,
#        generated KrakenD ConfigMaps, gateway, ingress, policies, and HPA.
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
#     cluster.local.env: K3S_API_HOST, K3S_SSH_USER, K3S_NODES, REGISTRY,
#     REGISTRY_SCHEME, and K3S_REMOTE_KUBECONFIG.
#   - cluster.local.env is intentionally not allowed to override identity or
#     model values. This keeps the repository on one public identity.
#
# RESET SAFETY:
#   reset/bootstrap refuse protected namespaces and delete only NAMESPACE. Image
#   build happens before namespace deletion, so a broken model build does not
#   destroy the currently running stack.
#
# PREREQUISITES:
#   - SSH access to the configured K3s API host.
#   - podman for image builds.
#   - kubectl for cluster operations.
#   - A container registry reachable by this machine and the K3s nodes.
#
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# ---------------------------------------------------------------------------
# Identity: edit here when the product/service identity changes.
# ---------------------------------------------------------------------------
# These are constants on purpose. Hidden identity overrides are what created the
# previous split-brain state, so cluster env files are not allowed to set them.
APP_NAME="sem-image-classifier"
SERVICE_NAME="$APP_NAME"
NAMESPACE="$APP_NAME"
IMAGE_REPOSITORY="$APP_NAME"
IMAGE_TAG="latest"
BENTOML_SERVICE="service:SEMInferenceRedisService"

# ---------------------------------------------------------------------------
# Cluster and registry defaults
# ---------------------------------------------------------------------------
# Environment files below may override these values for a concrete cluster.
K3S_API_HOST="${K3S_API_HOST:-127.0.0.1}"
K3S_SSH_USER="${K3S_SSH_USER:-root}"
K3S_REMOTE_KUBECONFIG="${K3S_REMOTE_KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"
K3S_NODES="${K3S_NODES:-}"
REGISTRY="${REGISTRY:-localhost:5000}"
REGISTRY_SCHEME="${REGISTRY_SCHEME:-http}"
IMAGE_NAME="${REGISTRY}/${IMAGE_REPOSITORY}:${IMAGE_TAG}"
SECRET_NAME="app-secrets"
DEPLOY_ENV="${DEPLOY_ENV:-dev}"
TUNNEL_SOCK="/tmp/k3s-api-tunnel.sock"
TUNNEL_KUBECONFIG="/tmp/k3s-tunnel-kubeconfig.yaml"
PF_PORT=8080
MANIFESTS_DIR="$SCRIPT_DIR/manifests"
GATEWAY_DIR="$PROJECT_DIR/gateway"
DB_DIR="$PROJECT_DIR/db"
ENV_BASE_DIR="$SCRIPT_DIR/env"
RELEASES_DIR="$SCRIPT_DIR/releases"

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

info()    { echo -e "${BLUE}[INFO]${NC} $*"; }
success() { echo -e "${GREEN}[  OK]${NC} $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
fail()    { echo -e "${RED}[FAIL]${NC} $*"; exit 1; }
step()    { echo -e "\n${CYAN}${BOLD}━━━ $* ━━━${NC}"; }

refresh_derived_config() {
    IMAGE_NAME="${REGISTRY}/${IMAGE_REPOSITORY}:${IMAGE_TAG}"
}

validate_cluster_env_file() {
    local file="$1"
    local invalid_keys
    invalid_keys="$(
        sed -nE 's/^[[:space:]]*(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)=.*/\2/p' "$file" \
            | grep -Ev '^(K3S_API_HOST|K3S_SSH_USER|K3S_REMOTE_KUBECONFIG|K3S_NODES|REGISTRY|REGISTRY_SCHEME)$' \
            || true
    )"
    if [ -n "$invalid_keys" ]; then
        fail "$file may contain only private infrastructure keys. Move identity to dev.sh and model values to bentoml-config*.yaml. Invalid keys: $(echo "$invalid_keys" | paste -sd ', ' -)"
    fi
}

source_cluster_env_file() {
    local file="$1"
    local label="$2"
    [ -f "$file" ] || return 0
    validate_cluster_env_file "$file"
    # shellcheck disable=SC1090
    source "$file"
    refresh_derived_config
    info "Loaded ${label}: $file"
}

validate_identity_contract() {
    # Keep the old project identity out of active files without spelling it as a
    # single literal here. Historical prompts/releases are excluded by design.
    command -v rg >/dev/null 2>&1 || return 0
    local legacy_pattern
    legacy_pattern='sem''-classifier'
    local matches
    matches="$(
        rg -n "$legacy_pattern" "$PROJECT_DIR" \
            --glob '!.git/**' \
            --glob '!internal_docs/**' \
            --glob '!k8s/releases/**' \
            --glob '!CLAUDE.md' \
            --glob '!.codex/**' \
            --glob '!**/__pycache__/**' \
            --glob '!**/.pytest_cache/**' \
            --glob '!**/.mypy_cache/**' \
            --glob '!**/.ruff_cache/**' \
            --glob '!**/.venv/**' \
            || true
    )"
    [ -z "$matches" ] || fail "Legacy identity found in active files:\n$matches"
}

usage() {
    echo -e "${BOLD}dev.sh - SEM Image Classifier K8s Development Tool${NC}"
    echo ""
    echo "  Usage: ./dev.sh <command> [args]"
    echo ""
    echo "  Commands:"
    echo "    config [--env dev|prod]"
    echo "    deploy [--env dev|prod] [--rebuild] [--use-release]"
    echo "    build-image [--env dev|prod]"
    echo "    build [--env dev|prod]"
    echo "    bootstrap [--env dev|prod]"
    echo "    status"
    echo "    access [--env dev|prod]"
    echo "    logs [component]"
    echo "    restart [--env dev|prod] <component>"
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
        fail "Kubeconfig not found: $KUBECONFIG\n  Run: ./dev.sh access"
    fi
    if ! kubectl cluster-info &>/dev/null 2>&1; then
        fail "Cannot connect to K3s. SSH tunnel may be down.\n  Run: ./dev.sh access"
    fi
}

ensure_namespace() {
    assert_safe_namespace
    kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -
    kubectl label namespace "$NAMESPACE" \
        app.kubernetes.io/name="$APP_NAME" \
        app.kubernetes.io/part-of="${APP_NAME}-stack" \
        --overwrite >/dev/null
    success "Namespace ready: $NAMESPACE"
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
    # Detached port-forwards survive after dev.sh exits. We verify the listener
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

resolve_environment_files() {
    # Environment resolution is intentionally centralized. Build-time model
    # inputs, Kubernetes overlays, and local cluster values are all loaded here
    # so subcommands cannot silently disagree.
    case "$DEPLOY_ENV" in
        dev|prod) ;;
        *) fail "Unsupported --env '$DEPLOY_ENV' (expected: dev|prod)" ;;
    esac

    ENV_DIR="$ENV_BASE_DIR/$DEPLOY_ENV"
    CLUSTER_CONFIG_FILE="$ENV_DIR/cluster.env"
    CLUSTER_LOCAL_CONFIG_FILE="$ENV_DIR/cluster.local.env"
    BENTOML_CONFIG_FILE="$ENV_DIR/bentoml-config.yaml"
    SECRETS_FILE="$ENV_DIR/secrets.yaml"
    GATEWAY_SETTINGS_FILE="$ENV_DIR/gateway-settings.json"

    source_cluster_env_file "$CLUSTER_CONFIG_FILE" "tracked cluster template"
    source_cluster_env_file "$CLUSTER_LOCAL_CONFIG_FILE" "local infrastructure overrides"
    validate_identity_contract

    [ -f "$ENV_DIR/bentoml-config.local.yaml" ] && BENTOML_CONFIG_FILE="$ENV_DIR/bentoml-config.local.yaml"
    [ -f "$ENV_DIR/secrets.local.yaml" ] && SECRETS_FILE="$ENV_DIR/secrets.local.yaml"
    [ -f "$ENV_DIR/gateway-settings.local.json" ] && GATEWAY_SETTINGS_FILE="$ENV_DIR/gateway-settings.local.json"

    [ -f "$BENTOML_CONFIG_FILE" ] || fail "Missing env config file: $BENTOML_CONFIG_FILE"
    [ -f "$SECRETS_FILE" ] || fail "Missing env secrets file: $SECRETS_FILE"
    [ -f "$GATEWAY_SETTINGS_FILE" ] || fail "Missing env gateway settings file: $GATEWAY_SETTINGS_FILE"

    if [[ "$BENTOML_CONFIG_FILE" == *.local.yaml ]] || [[ "$SECRETS_FILE" == *.local.yaml ]] || [[ "$GATEWAY_SETTINGS_FILE" == *.local.json ]]; then
        info "Using local overrides from $ENV_DIR (*.local.*)"
    fi

    if [ "$DEPLOY_ENV" = "prod" ]; then
        grep -q "REPLACE_WITH_PROD_" "$SECRETS_FILE" && fail "Production secrets still contain unreplaced values: $SECRETS_FILE"
        grep -q "auth.example.com" "$GATEWAY_SETTINGS_FILE" && fail "Production gateway settings still contain example auth URLs: $GATEWAY_SETTINGS_FILE"
    fi

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
        if [ "$DEPLOY_ENV" = "prod" ] && ! [[ "$MODEL_REVISION" =~ ^[0-9a-f]{40}$ ]]; then
            fail "Production MODEL_REVISION must be an immutable 40-char commit SHA (got: $MODEL_REVISION)"
        fi
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
    RELEASE_FILE="$RELEASES_DIR/release-${DEPLOY_ENV}-${ts}.json"

    cat > "$RELEASE_FILE" <<JSON
{
  "created_at_utc": "$ts",
  "deploy_env": "$DEPLOY_ENV",
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

    ln -sfn "$(basename "$RELEASE_FILE")" "$RELEASES_DIR/release-${DEPLOY_ENV}-latest.json"
    success "Release artifact written: $RELEASE_FILE"
}

load_latest_release_artifact() {
    local latest_file="$RELEASES_DIR/release-${DEPLOY_ENV}-latest.json"
    [ -f "$latest_file" ] || fail "Missing release artifact: $latest_file (run ./dev.sh build-image --env $DEPLOY_ENV first)"

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

image_tag_exists_in_registry() {
    local curl_args=(-sf)
    [ -f /etc/registry/certs/ca.crt ] && curl_args+=(--cacert /etc/registry/certs/ca.crt)
    curl "${curl_args[@]}" "${REGISTRY_SCHEME}://${REGISTRY}/v2/${IMAGE_REPOSITORY}/tags/list" \
        | grep -q "\"${IMAGE_TAG}\"" &>/dev/null 2>&1
}

render_gateway_settings() {
    # Gateway settings stay JSON, but SERVICE_NAME is script-owned so a model
    # migration does not require editing gateway templates or plugin code.
    RENDERED_GATEWAY_SETTINGS_FILE="/tmp/${APP_NAME}-${DEPLOY_ENV}-gateway-settings.json"
    python3 - "$GATEWAY_SETTINGS_FILE" "$RENDERED_GATEWAY_SETTINGS_FILE" "$SERVICE_NAME" <<'PY'
import json
import sys

source, target, service_name = sys.argv[1:4]
with open(source, "r", encoding="utf-8") as fh:
    settings = json.load(fh)

settings["service_name"] = service_name

with open(target, "w", encoding="utf-8") as fh:
    json.dump(settings, fh, indent=2)
    fh.write("\n")
PY
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

    step "Pushing image to configured registry (${REGISTRY})"
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
    render_gateway_settings

    kubectl create configmap krakend-template \
        --from-file=krakend.tmpl="$GATEWAY_DIR/krakend.tmpl" \
        -n "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -
    success "krakend-template"

    kubectl create configmap krakend-settings \
        --from-file=settings.json="$RENDERED_GATEWAY_SETTINGS_FILE" \
        -n "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -
    success "krakend-settings"

    local template_args=()
    for f in "$GATEWAY_DIR/templates/"*.tmpl; do
        [ -f "$f" ] && template_args+=(--from-file="$f")
    done
    if [ ${#template_args[@]} -gt 0 ]; then
        kubectl create configmap krakend-templates \
            "${template_args[@]}" \
            -n "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -
    else
        kubectl create configmap krakend-templates \
            -n "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -
    fi
    success "krakend-templates"

    local partial_args=()
    for f in "$GATEWAY_DIR/partials/"*; do
        [ -f "$f" ] && partial_args+=(--from-file="$f")
    done
    kubectl create configmap krakend-partials \
        "${partial_args[@]}" \
        -n "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -
    success "krakend-partials"

    if [ -d "$GATEWAY_DIR/plugin" ] && [ -f "$GATEWAY_DIR/plugin/main.go" ]; then
        local plugin_args=()
        for f in "$GATEWAY_DIR/plugin/"*; do
            [ -f "$f" ] && plugin_args+=(--from-file="$f")
        done
        kubectl create configmap krakend-plugin \
            "${plugin_args[@]}" \
            -n "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -
        success "krakend-plugin"
    fi

    if [ -d "$DB_DIR" ] && [ -f "$DB_DIR/schema.sql" ]; then
        kubectl create configmap postgresql-init \
            --from-file="$DB_DIR/schema.sql" \
            -n "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -
        success "postgresql-init"
    fi
}

apply_manifest() {
    local manifest="$1"
    kubectl apply -n "$NAMESPACE" -f "$manifest" && success "Applied $(basename "$manifest")"
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
    while [ $# -gt 0 ]; do
        case "$1" in
            --env) DEPLOY_ENV="${2:-}"; [ -n "$DEPLOY_ENV" ] || fail "Missing value for --env"; shift ;;
            *) fail "Unknown config argument: $1" ;;
        esac
        shift
    done
    resolve_environment_files

    step "Resolved configuration"
    cat <<EOF
Environment:              $DEPLOY_ENV
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
    # Redis/PostgreSQL first, model backend next, gateway last.
    local rebuild=false
    local use_release=false
    while [ $# -gt 0 ]; do
        case "$1" in
            --env) DEPLOY_ENV="${2:-}"; [ -n "$DEPLOY_ENV" ] || fail "Missing value for --env"; shift ;;
            --rebuild) rebuild=true ;;
            --use-release) use_release=true ;;
            *) fail "Unknown deploy argument: $1" ;;
        esac
        shift
    done

    resolve_environment_files
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
    elif image_tag_exists_in_registry; then
        success "Image tag exists in registry"
        if [ -f "$RELEASES_DIR/release-${DEPLOY_ENV}-latest.json" ]; then
            load_latest_release_artifact
        else
            warn "No release artifact found; deploying mutable tag $IMAGE_NAME"
            IMAGE_DEPLOY_REF="$IMAGE_NAME"
        fi
    else
        build_and_distribute
    fi

    step "Applying K8s resources"
    ensure_namespace

    kubectl apply -n "$NAMESPACE" -f "$SECRETS_FILE" && success "Applied $(basename "$SECRETS_FILE")"
    kubectl apply -n "$NAMESPACE" -f "$BENTOML_CONFIG_FILE" && success "Applied $(basename "$BENTOML_CONFIG_FILE")"

    apply_manifest "$MANIFESTS_DIR/01-redis.yaml"
    apply_manifest "$MANIFESTS_DIR/02-bentoml.yaml"
    apply_manifest "$MANIFESTS_DIR/03-postgresql.yaml"

    if [ "$DEPLOY_ENV" = "dev" ]; then
        apply_manifest "$MANIFESTS_DIR/04-mock-oidc.yaml"
    else
        info "Skipping mock-oidc for env=$DEPLOY_ENV"
    fi

    generate_gateway_configmaps
    apply_manifest "$MANIFESTS_DIR/05-krakend.yaml"
    apply_manifest "$MANIFESTS_DIR/06-ingress.yaml"
    apply_manifest "$MANIFESTS_DIR/07-networkpolicies.yaml"
    apply_manifest "$MANIFESTS_DIR/08-hpa.yaml"

    step "Waiting for pods"
    kubectl rollout status statefulset/redis -n "$NAMESPACE" --timeout=120s || fail "Redis failed"
    kubectl rollout status statefulset/postgresql -n "$NAMESPACE" --timeout=120s || fail "PostgreSQL failed"

    if [ -n "$IMAGE_DEPLOY_REF" ]; then
        info "Setting BentoML deployment image to $IMAGE_DEPLOY_REF"
        kubectl set image deployment/bentoml bentoml="$IMAGE_DEPLOY_REF" -n "$NAMESPACE" >/dev/null
    fi
    kubectl rollout status deployment/bentoml -n "$NAMESPACE" --timeout=600s || fail "BentoML failed"
    kubectl rollout status deployment/krakend -n "$NAMESPACE" --timeout=300s || fail "KrakenD failed"

    success "Deployment complete"
    kubectl get pods -n "$NAMESPACE" -o wide
    print_access_instructions
}

build_image() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --env) DEPLOY_ENV="${2:-}"; [ -n "$DEPLOY_ENV" ] || fail "Missing value for --env"; shift ;;
            *) fail "Unknown build-image argument: $1" ;;
        esac
        shift
    done
    resolve_environment_files
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
    stop_local_port_forward "18080"

    kubectl delete namespace "$NAMESPACE" --ignore-not-found=true --timeout=120s
    success "Namespace reset request completed: $NAMESPACE"
}

bootstrap() {
    # Safe reset flow: build and push image, write release artifact, then reset
    # namespace and deploy from that immutable image reference.
    while [ $# -gt 0 ]; do
        case "$1" in
            --env) DEPLOY_ENV="${2:-}"; [ -n "$DEPLOY_ENV" ] || fail "Missing value for --env"; shift ;;
            *) fail "Unknown bootstrap argument: $1" ;;
        esac
        shift
    done
    resolve_environment_files
    ensure_kubeconfig
    build_and_distribute
    reset_namespace --yes
    deploy --env "$DEPLOY_ENV" --use-release
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
    while [ $# -gt 0 ]; do
        case "$1" in
            --env) DEPLOY_ENV="${2:-}"; [ -n "$DEPLOY_ENV" ] || fail "Missing value for --env"; shift ;;
            *) fail "Unknown access argument: $1" ;;
        esac
        shift
    done
    resolve_environment_files
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

    start_service_port_forward "krakend" "$PF_PORT" "8080" "/tmp/pf-krakend.log"

    if [ "$DEPLOY_ENV" = "dev" ] && kubectl get svc/mock-oidc -n "$NAMESPACE" &>/dev/null 2>&1; then
        start_service_port_forward "mock-oidc" "18080" "8080" "/tmp/pf-mock-oidc.log"
    fi
    success "Access ready"
    print_access_instructions
}

print_access_instructions() {
    echo ""
    echo -e "  ${BOLD}Local tunnel:${NC} ssh -L ${PF_PORT}:localhost:${PF_PORT} -L 18080:localhost:18080 ${K3S_SSH_USER}@${K3S_API_HOST}"
    echo -e "  ${BOLD}Health:${NC} curl http://localhost:${PF_PORT}/__health"
    echo -e "  ${BOLD}Token:${NC} cd k8s && ./dev.sh token testuser"
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
    while [ $# -gt 0 ]; do
        case "$1" in
            --env) DEPLOY_ENV="${2:-}"; [ -n "$DEPLOY_ENV" ] || fail "Missing value for --env"; shift ;;
            *) fail "Unknown restart argument: $1" ;;
        esac
        shift
    done
    resolve_environment_files
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
    local username="${1:-testuser}"
    assert_safe_namespace
    ensure_kubeconfig
    local mock_port=18080
    if ! ss -tlnp 2>/dev/null | grep -q ":${mock_port} "; then
        start_service_port_forward "mock-oidc" "$mock_port" "8080" "/tmp/pf-mock-oidc.log"
    fi
    local response
    response="$(curl -s -X POST "http://localhost:${mock_port}/default/token" \
        -H "Host: mock-oidc:8080" \
        -H "Content-Type: application/x-www-form-urlencoded" \
            -d "grant_type=client_credentials&scope=openid" \
            -d "client_id=${username}" \
            -d "client_secret=test-secret" 2>/dev/null)"
    local token
    token="$(echo "$response" | python3 -c "import sys,json; print(json.load(sys.stdin).get('access_token',''))" 2>/dev/null)"
    [ -n "$token" ] || fail "Failed to get token from mock-oidc. Response: $response"
    echo "$token"
}

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
    restart) [ -n "${1:-}" ] || fail "Usage: ./dev.sh restart <component>"; restart_component "$@" ;;
    token) get_token "${1:-testuser}" ;;
    reset) [ "${1:-}" = "--yes" ] && reset_namespace --yes || reset_namespace ;;
    cleanup) reset_namespace ;;
    *) usage; exit 1 ;;
esac
