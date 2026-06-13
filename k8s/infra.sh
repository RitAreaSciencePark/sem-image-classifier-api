#!/bin/bash
# ==============================================================================
# infra.sh — Infrastructure Layer (Authentik Identity Services)
# ==============================================================================
#
# Manages Authentik in authentik-reusable-ml-services namespace.
# Dev-environment operator only — see docs/dev-environment-setup.md.
# Deploy before the API stack: ./infra.sh deploy && ./app.sh deploy
#
# USAGE:
#   ./infra.sh deploy [--skip-config]
#   ./infra.sh configure
#   ./infra.sh status
#   ./infra.sh logs [authentik-server|authentik-worker]
#   ./infra.sh restart <component>
#   ./infra.sh cleanup
#   ./infra.sh check
#
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

INFRA_NAMESPACE="authentik-reusable-ml-services"

INFRA_MANIFESTS_DIR="$SCRIPT_DIR/manifests/infra"
ENV_BASE_DIR="$SCRIPT_DIR/env"

K3S_API_HOST="${K3S_API_HOST:-127.0.0.1}"
K3S_SSH_USER="${K3S_SSH_USER:-root}"
K3S_REMOTE_KUBECONFIG="${K3S_REMOTE_KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"
K3S_NODES="${K3S_NODES:-}"
TUNNEL_SOCK="/tmp/k3s-api-tunnel.sock"
TUNNEL_KUBECONFIG="/tmp/k3s-tunnel-kubeconfig.yaml"

ENV_DIR=""
INFRA_SECRETS_FILE=""
AUTHENTIK_SERVICE_FILE=""

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

rollout_with_retry() {
    local resource="$1"
    local timeout="${2:-120s}"
    local attempts="${3:-2}"
    local ns="${4:-$INFRA_NAMESPACE}"
    local attempt

    for ((attempt=1; attempt<=attempts; attempt++)); do
        if kubectl rollout status "$resource" -n "$ns" --timeout="$timeout"; then
            return 0
        fi
        warn "$resource rollout attempt ${attempt}/${attempts} failed (ns=$ns)"
        [ "$attempt" -lt "$attempts" ] && info "Retrying rollout wait for $resource..."
    done
    fail "$resource rollout did not complete after ${attempts} attempts (ns=$ns)"
}

wait_for_rollout() {
    local resource="$1"
    local timeout="${2:-300s}"
    local attempts="${3:-2}"
    local ns="${4:-$INFRA_NAMESPACE}"
    info "Waiting for $resource (ns=$ns)..."
    rollout_with_retry "$resource" "$timeout" "$attempts" "$ns"
    success "$resource is ready"
}

get_secret_value() {
    kubectl get secret "$1" -n "$3" -o "jsonpath={.data.${2}}" | base64 -d
}

ensure_kubeconfig() {
    export KUBECONFIG="${KUBECONFIG:-$TUNNEL_KUBECONFIG}"
    [ -f "$KUBECONFIG" ] || fail "Kubeconfig not found: $KUBECONFIG\n  Run: ./app.sh access"
    kubectl cluster-info &>/dev/null 2>&1 || fail "Cannot connect to K3s. Run: ./app.sh access"
}

resolve_infra_environment_files() {
    ENV_DIR="$ENV_BASE_DIR/dev"
    INFRA_SECRETS_FILE="$ENV_DIR/infra-secrets.yaml"

    local infra_secrets_local="$ENV_DIR/infra-secrets.local.yaml"
    [ -f "$infra_secrets_local" ] && INFRA_SECRETS_FILE="$infra_secrets_local"
    [ -f "$INFRA_SECRETS_FILE" ] || fail "Missing infra secrets file: $INFRA_SECRETS_FILE"


    [[ "$INFRA_SECRETS_FILE" == *.local.yaml ]] && info "Using local infra secrets: $INFRA_SECRETS_FILE"
}

apply_infra_manifests_for_env() {
    step "Deploying Authentik infra ($INFRA_NAMESPACE)"

    kubectl apply -f "$INFRA_MANIFESTS_DIR/00-namespace.yaml"
    kubectl apply -f "$INFRA_SECRETS_FILE"

    kubectl apply -f "$INFRA_MANIFESTS_DIR/01-authentik-postgres.yaml"
    kubectl apply -f "$INFRA_MANIFESTS_DIR/02-authentik-redis.yaml"
    wait_for_rollout "deployment/authentik-postgres" "300s" "2" "$INFRA_NAMESPACE"
    wait_for_rollout "deployment/authentik-redis" "120s" "2" "$INFRA_NAMESPACE"

    kubectl apply -f "$INFRA_MANIFESTS_DIR/03-authentik-server.yaml"
    wait_for_rollout "deployment/authentik-server" "300s" "2" "$INFRA_NAMESPACE"
    wait_for_rollout "deployment/authentik-worker" "300s" "2" "$INFRA_NAMESPACE"

    success "Authentik infra manifests applied"
}

configure_authentik_from_cluster() {
    step "Configuring Authentik OAuth2 provider (M2M)"

    local service_id="${SERVICE:-sem-classifier}"
    local project_dir
    project_dir="$(dirname "$SCRIPT_DIR")"
    local deploy_env="$project_dir/services/$service_id/generated/dev/deploy.env"
    [ -f "$deploy_env" ] || fail "Missing $deploy_env — run: make render SERVICE=$service_id"
    # shellcheck disable=SC1090
    source "$deploy_env"
    APP_NAMESPACE="$NAMESPACE"

    local authentik_pod
    authentik_pod="$(kubectl get pods -n "$INFRA_NAMESPACE" -l app=authentik-server \
        --no-headers -o custom-columns=":metadata.name" | head -1)"
    [ -n "$authentik_pod" ] || fail "Authentik server pod not found in $INFRA_NAMESPACE"

    if ! kubectl get namespace "$APP_NAMESPACE" &>/dev/null 2>&1; then
        warn "App namespace '$APP_NAMESPACE' not found — skipping OAuth2 configuration."
        warn "Deploy the API first, then run: ./infra.sh configure"
        return 0
    fi

    if ! kubectl get secret app-secrets -n "$APP_NAMESPACE" &>/dev/null 2>&1; then
        warn "app-secrets not found in $APP_NAMESPACE — skipping OAuth2 configuration."
        warn "Deploy the API first, then run: ./infra.sh configure"
        return 0
    fi

    local oidc_secret oidc_client_id bootstrap_password oidc_slug
    oidc_secret="$(get_secret_value app-secrets oidc-client-secret "$APP_NAMESPACE")"
    oidc_client_id="$(get_secret_value app-secrets oidc-client-id "$APP_NAMESPACE")"
    bootstrap_password="$(get_secret_value authentik-secret bootstrap-password "$INFRA_NAMESPACE")"
    oidc_slug="${OIDC_APPLICATION_SLUG:-$service_id}"
    local provider_name="${OIDC_PROVIDER_NAME:-${service_id}-provider}"
    local app_display_name="${OIDC_APP_DISPLAY_NAME:-$service_id API}"

    [ -n "$oidc_secret" ] || fail "oidc-client-secret is empty in app-secrets"
    [ -n "$oidc_client_id" ] || fail "oidc-client-id is empty in app-secrets"

    kubectl cp "$SCRIPT_DIR/configure_authentik.py" \
        "$INFRA_NAMESPACE/$authentik_pod:/tmp/configure_authentik.py"

    kubectl exec -n "$INFRA_NAMESPACE" "$authentik_pod" -- env \
        OIDC_CLIENT_ID="$oidc_client_id" \
        OIDC_CLIENT_SECRET="$oidc_secret" \
        OIDC_APPLICATION_SLUG="$oidc_slug" \
        OIDC_PROVIDER_NAME="$provider_name" \
        OIDC_APP_DISPLAY_NAME="$app_display_name" \
        AUTHENTIK_BOOTSTRAP_PASSWORD="$bootstrap_password" \
        python /tmp/configure_authentik.py

    if kubectl get deployment krakend -n "$APP_NAMESPACE" &>/dev/null 2>&1; then
        kubectl rollout restart deployment/krakend -n "$APP_NAMESPACE"
        kubectl rollout status deployment/krakend -n "$APP_NAMESPACE" --timeout=300s
        success "Authentik configured and KrakenD restarted"
    else
        success "Authentik configured — KrakenD will pick up JWKS on first deploy"
        info "Hint: run ./app.sh deploy"
    fi
}

deploy_infra() {
    local skip_config=false
    while [ $# -gt 0 ]; do
        case "$1" in
            --skip-config) skip_config=true ;;
            -h|--help)
                echo "Usage: ./infra.sh deploy [--skip-config]"
                return 0
                ;;
            *) fail "Unknown argument: $1" ;;
        esac
        shift
    done

    resolve_infra_environment_files
    ensure_kubeconfig
    step "Infra deploy start"
    apply_infra_manifests_for_env

    warn "OAuth2 is configured per service: make infra-configure SERVICE=<id>"

    success "Infra deploy completed"
    echo ""
    echo "  Authentik namespace : $INFRA_NAMESPACE"
    echo "  Next step           : ./app.sh deploy [--rebuild]"
    echo ""
}

run_configure() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --service)
                SERVICE="${2:-}"
                shift 2
                ;;
            *)
                shift
                ;;
        esac
    done
    resolve_infra_environment_files
    ensure_kubeconfig
    configure_authentik_from_cluster
}

show_status() {
    ensure_kubeconfig
    step "Authentik Pods ($INFRA_NAMESPACE)"
    kubectl get pods -n "$INFRA_NAMESPACE" -o wide 2>/dev/null || warn "Namespace not deployed yet"
    step "SSH Tunnel"
    [ -S "$TUNNEL_SOCK" ] && success "K3s API tunnel active" || warn "K3s API tunnel not running"
}

show_logs() {
    ensure_kubeconfig
    local component="${1:-authentik-server}"
    case "$component" in
        authentik-server|authentik-worker)
            kubectl logs -l "app=$component" -n "$INFRA_NAMESPACE" --tail=50 -f
            ;;
        *) fail "Unknown component: $component (use authentik-server or authentik-worker)" ;;
    esac
}

restart_component() {
    local component="$1"
    ensure_kubeconfig
    kubectl rollout restart "deployment/$component" -n "$INFRA_NAMESPACE"
    rollout_with_retry "deployment/$component" "180s" "2" "$INFRA_NAMESPACE"
    success "$component restarted"
}

run_cleanup() {
    ensure_kubeconfig
    warn "Deleting namespace '$INFRA_NAMESPACE' and all its resources..."
    kubectl delete namespace "$INFRA_NAMESPACE" --ignore-not-found --timeout=120s 2>/dev/null || true
    success "Namespace $INFRA_NAMESPACE deleted"
}

run_check() {
    local errors=0
    step "Infrastructure health check"

    if [ -n "$K3S_NODES" ]; then
        local node
        for node in $K3S_NODES; do
            if ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no -o BatchMode=yes \
                "${K3S_SSH_USER}@${node}" "echo ok" >/dev/null 2>&1; then
                success "SSH reachable: $node"
            else
                warn "SSH not reachable: $node"
                errors=$((errors + 1))
            fi
        done
    fi

    ensure_kubeconfig 2>/dev/null || { fail "K3s API unreachable"; return; }

    if kubectl get namespace "$INFRA_NAMESPACE" &>/dev/null 2>&1; then
        local ready
        ready="$(kubectl get pods -n "$INFRA_NAMESPACE" --field-selector=status.phase=Running --no-headers 2>/dev/null | wc -l | tr -d ' ')"
        success "Authentik namespace running pods: $ready"
    else
        warn "Authentik namespace not deployed"
        errors=$((errors + 1))
    fi

    if kubectl get namespace "$APP_NAMESPACE" &>/dev/null 2>&1; then
        local app_ready
        app_ready="$(kubectl get pods -n "$APP_NAMESPACE" --field-selector=status.phase=Running --no-headers 2>/dev/null | wc -l | tr -d ' ')"
        success "App namespace running pods: $app_ready"
    else
        info "App namespace not deployed yet"
    fi

    [ "$errors" -eq 0 ] && success "Health check passed" || warn "Health check completed with $errors issue(s)"
}

usage() {
    echo -e "${BOLD}infra.sh - Authentik Infrastructure${NC}"
    echo ""
    echo "  ./infra.sh deploy [--skip-config]"
    echo "  ./infra.sh configure"
    echo "  ./infra.sh status | logs | restart | cleanup | check"
}

COMMAND="${1:-}"
[ -n "$COMMAND" ] || { usage; exit 1; }
shift || true

case "$COMMAND" in
    deploy) deploy_infra "$@" ;;
    configure) run_configure "$@" ;;
    status) show_status ;;
    logs) show_logs "${1:-authentik-server}" ;;
    restart)
        [ -n "${1:-}" ] || fail "Usage: ./infra.sh restart <component>"
        restart_component "$1"
        ;;
    cleanup) run_cleanup ;;
    check) run_check ;;
    *) usage; exit 1 ;;
esac
