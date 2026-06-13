#!/bin/bash
# ==============================================================================
# render-gateway-configmaps.sh — DEV ONLY (Stencil cluster convenience)
# ==============================================================================
#
# Called by k8s/app.sh during development deploys. Production operators do NOT
# use this script — see docs/production-deployment.md for manual kubectl create
# configmap commands.
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
GATEWAY_DIR="$PROJECT_DIR/gateway"
DB_DIR="$PROJECT_DIR/db"
NAMESPACE=""
GATEWAY_SETTINGS_FILE=""
SERVICE_NAME=""

usage() {
    cat <<EOF
Usage: ./render-gateway-configmaps.sh --namespace <ns> \\
       [--gateway-settings <path>] [--service-name <name>]

Generates KrakenD flexible-configuration ConfigMaps in the dev cluster namespace.
Production deploys use manual kubectl — see docs/production-deployment.md.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help) usage; exit 0 ;;
        --namespace) NAMESPACE="${2:-}"; shift 2 ;;
        --gateway-settings) GATEWAY_SETTINGS_FILE="${2:-}"; shift 2 ;;
        --service-name) SERVICE_NAME="${2:-}"; shift 2 ;;
        *) echo "Unknown argument: $1" >&2; usage; exit 1 ;;
    esac
done

[ -n "$NAMESPACE" ] || { echo "Missing --namespace" >&2; usage; exit 1; }

[ -n "$GATEWAY_SETTINGS_FILE" ] || { echo "Missing --gateway-settings (no env fallbacks)" >&2; usage; exit 1; }
[ -f "$GATEWAY_SETTINGS_FILE" ] || { echo "Gateway settings not found: $GATEWAY_SETTINGS_FILE" >&2; exit 1; }

RENDERED_GATEWAY_SETTINGS_FILE="/tmp/${SERVICE_NAME}-gateway-settings.json"
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

kubectl create configmap krakend-template \
    --from-file=krakend.tmpl="$GATEWAY_DIR/krakend.tmpl" \
    -n "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -

kubectl create configmap krakend-settings \
    --from-file=settings.json="$RENDERED_GATEWAY_SETTINGS_FILE" \
    -n "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -

template_args=()
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

partial_args=()
for f in "$GATEWAY_DIR/partials/"*; do
    [ -f "$f" ] && partial_args+=(--from-file="$f")
done
kubectl create configmap krakend-partials \
    "${partial_args[@]}" \
    -n "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -

if [ -d "$GATEWAY_DIR/plugin" ] && [ -f "$GATEWAY_DIR/plugin/main.go" ]; then
    plugin_args=()
    for f in "$GATEWAY_DIR/plugin/"*; do
        [ -f "$f" ] && plugin_args+=(--from-file="$f")
    done
    kubectl create configmap krakend-plugin \
        "${plugin_args[@]}" \
        -n "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -
fi

if [ -d "$DB_DIR" ] && [ -f "$DB_DIR/schema.sql" ]; then
    kubectl create configmap postgresql-init \
        --from-file="$DB_DIR/schema.sql" \
        -n "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -
fi

echo "KrakenD ConfigMaps applied to namespace $NAMESPACE"
