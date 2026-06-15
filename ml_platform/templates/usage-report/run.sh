#!/usr/bin/env bash
# API usage report — kubectl + bash + python3 (HTML output).
#
# Reads api_usage from postgresql-0 via kubectl exec and writes a uniquely named
# HTML report file encoding all query parameters in the filename and report_meta.
#
# Usage:
#   ./run.sh --namespace <ns> [--context <ctx>] [--since 24h] [--until now] \
#     [--bucket auto|minute|hour|day] [--timezone UTC] [--format html|json|summary] \
#     [--output-dir .] [--postgres-pod postgresql-0] [--kubectl kubectl]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

NS=""
KUBE_CONTEXT=""
SINCE="24h"
UNTIL="now"
BUCKET="auto"
TIMEZONE="UTC"
FORMAT="html"
OUTPUT_DIR="."
POSTGRES_POD="postgresql-0"
KUBECTL="kubectl"
RECENT_LIMIT=25
MODE="report"

usage() {
  sed -n '2,12p' "$0" | sed 's/^# \?//'
  exit "${1:-0}"
}

positive_int() {
  if ! [[ "$1" =~ ^[1-9][0-9]*$ ]]; then
    echo "ERROR: --recent-limit must be a positive integer" >&2
    exit 1
  fi
}

sql_literal() {
  printf "'%s'" "${1//\'/\'\'}"
}

time_expr() {
  local value="${1:-}"
  local default_now="${2:-false}"
  local normalized amount unit unit_name

  if [[ -z "$value" ]]; then
    if [[ "$default_now" == "true" ]]; then
      echo "NOW()"
    else
      echo "NULL::timestamptz"
    fi
    return
  fi

  normalized="$(echo "$value" | tr '[:upper:]' '[:lower:]')"
  if [[ "$normalized" == "now" || "$normalized" == "all" ]]; then
    if [[ "$default_now" == "true" ]]; then
      echo "NOW()"
    else
      echo "NULL::timestamptz"
    fi
    return
  fi

  if [[ "$normalized" =~ ^([0-9]+)(s|m|h|d|w)$ ]]; then
    amount="${BASH_REMATCH[1]}"
    unit="${BASH_REMATCH[2]}"
    case "$unit" in
      s) unit_name="seconds" ;;
      m) unit_name="minutes" ;;
      h) unit_name="hours" ;;
      d) unit_name="days" ;;
      w) unit_name="weeks" ;;
    esac
    echo "NOW() - interval $(sql_literal "$amount $unit_name")"
    return
  fi

  echo "$(sql_literal "$value")::timestamptz"
}

choose_bucket() {
  local mode="$1"
  local since="$2"
  local bucket="$3"
  local normalized amount unit seconds

  if [[ "$bucket" != "auto" ]]; then
    echo "$bucket"
    return
  fi

  if [[ "$mode" == "report" && -z "$since" ]]; then
    echo "day"
    return
  fi

  if [[ -n "$since" ]]; then
    normalized="$(echo "$since" | tr '[:upper:]' '[:lower:]')"
    if [[ "$normalized" =~ ^([0-9]+)(s|m|h|d|w)$ ]]; then
      amount="${BASH_REMATCH[1]}"
      unit="${BASH_REMATCH[2]}"
      case "$unit" in
        s) seconds="$amount" ;;
        m) seconds=$((amount * 60)) ;;
        h) seconds=$((amount * 3600)) ;;
        d) seconds=$((amount * 86400)) ;;
        w) seconds=$((amount * 604800)) ;;
      esac
      if (( seconds <= 6 * 3600 )); then
        echo "minute"
        return
      fi
      if (( seconds <= 7 * 86400 )); then
        echo "hour"
        return
      fi
    fi
  fi

  if [[ "$mode" == "report" ]]; then
    echo "day"
  else
    echo "hour"
  fi
}

sanitize_label() {
  echo "$1" | tr '/\\ :' '-----' | sed 's/--*/-/g; s/^-//; s/-$//'
}

report_filename() {
  local ns="$1" since="$2" until="$3" bucket="$4" tz="$5" fmt="$6"
  local stamp ext
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  case "$fmt" in
    json) ext="json" ;;
    summary) ext="txt" ;;
    html|*) ext="html" ;;
  esac
  printf 'api-usage_%s_since-%s_until-%s_bucket-%s_tz-%s_%s.%s\n' \
    "$(sanitize_label "$ns")" \
    "$(sanitize_label "$since")" \
    "$(sanitize_label "$until")" \
    "$(sanitize_label "$bucket")" \
    "$(sanitize_label "$tz")" \
    "$stamp" \
    "$ext"
}

load_default_namespace() {
  local deploy_env="$SCRIPT_DIR/../deploy.env"
  if [[ -z "$NS" && -f "$deploy_env" ]]; then
    # shellcheck disable=SC1090
    NS="$(grep '^NAMESPACE=' "$deploy_env" | cut -d= -f2- | tr -d '"')"
  fi
}

substitute_sql() {
  local template="$1"
  local start_expr="$2"
  local end_expr="$3"
  local tz_lit="$4"
  local bucket_lit="$5"
  local recent="$6"
  local meta_ns="$7"
  local meta_since="$8"
  local meta_until="$9"
  local meta_pod="${10}"
  local meta_ctx="${11}"

  sed \
    -e "s|__START_EXPR__|${start_expr}|g" \
    -e "s|__END_EXPR__|${end_expr}|g" \
    -e "s|__TZ__|${tz_lit}|g" \
    -e "s|__BUCKET__|${bucket_lit}|g" \
    -e "s|__RECENT_LIMIT__|${recent}|g" \
    -e "s|__META_NAMESPACE__|${meta_ns}|g" \
    -e "s|__META_SINCE__|${meta_since}|g" \
    -e "s|__META_UNTIL__|${meta_until}|g" \
    -e "s|__META_POSTGRES_POD__|${meta_pod}|g" \
    -e "s|__META_KUBE_CONTEXT__|${meta_ctx}|g" \
    "$template"
}

write_json_file() {
  local dest="$1"
  local line="$2"
  printf '%s\n' "$line" | python3 -m json.tool 2>/dev/null > "$dest" || printf '%s\n' "$line" > "$dest"
}

render_html_from_json() {
  local json_file="$1"
  local html_file="$2"
  local ns="$3"
  if [[ ! -f "$SCRIPT_DIR/usage_report.py" ]]; then
    echo "ERROR: missing $SCRIPT_DIR/usage_report.py (run make render)" >&2
    exit 1
  fi
  if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 is required for HTML reports (install Python 3 or use --format json)" >&2
    exit 1
  fi
  python3 "$SCRIPT_DIR/usage_report.py" report --input "$json_file" --output "$html_file" --namespace "$ns" >/dev/null
}

render_summary_from_json() {
  local json_file="$1"
  local ns="$2"
  if [[ -f "$SCRIPT_DIR/usage_report.py" ]] && command -v python3 >/dev/null 2>&1; then
    python3 "$SCRIPT_DIR/usage_report.py" summary --input "$json_file" --namespace "$ns"
    return
  fi
  if command -v jq >/dev/null 2>&1; then
    jq -r '
      "API Usage Summary",
      "Namespace: \(.report_meta.namespace // "unknown")",
      "Generated UTC: \(.report_meta.generated_at_utc // .generated_at_utc // "")",
      "Since: \(.report_meta.since // "") | Until: \(.report_meta.until // "")",
      "Timezone: \(.report_meta.timezone // .timezone // "UTC") | Bucket: \(.report_meta.bucket // .bucket // "")",
      "",
      "Total requests: \(.total // 0)",
      "Unique users  : \(.unique_users // 0)",
      "Errors        : \(.error_count // 0)"
    ' "$json_file"
    return
  fi
  echo "ERROR: --format summary requires python3 (usage_report.py) or jq" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage 0 ;;
    --namespace) NS="$2"; shift 2 ;;
    --context) KUBE_CONTEXT="$2"; shift 2 ;;
    --since) SINCE="$2"; shift 2 ;;
    --until) UNTIL="$2"; shift 2 ;;
    --bucket) BUCKET="$2"; shift 2 ;;
    --timezone) TIMEZONE="$2"; shift 2 ;;
    --format) FORMAT="$2"; shift 2 ;;
    --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
    --postgres-pod) POSTGRES_POD="$2"; shift 2 ;;
    --kubectl) KUBECTL="$2"; shift 2 ;;
    --recent-limit) RECENT_LIMIT="$2"; positive_int "$RECENT_LIMIT"; shift 2 ;;
    --mode) MODE="$2"; shift 2 ;;
    *) echo "ERROR: unknown argument: $1" >&2; usage 1 ;;
  esac
done

load_default_namespace

if [[ -z "$NS" ]]; then
  echo "ERROR: --namespace is required (or set NAMESPACE in ../deploy.env)" >&2
  exit 1
fi

if [[ "$FORMAT" != "json" && "$FORMAT" != "summary" && "$FORMAT" != "html" ]]; then
  echo "ERROR: --format must be html, json, or summary" >&2
  exit 1
fi

if [[ ! -f "$SCRIPT_DIR/report.sql" ]]; then
  echo "ERROR: missing $SCRIPT_DIR/report.sql (run make render)" >&2
  exit 1
fi

RESOLVED_BUCKET="$(choose_bucket "$MODE" "$SINCE" "$BUCKET")"
SINCE_LABEL="$SINCE"
UNTIL_LABEL="$UNTIL"
[[ -z "$SINCE" || "$SINCE" == "all" ]] && SINCE_LABEL="all"
[[ -z "$UNTIL" ]] && UNTIL_LABEL="now"

START_EXPR="$(time_expr "$SINCE" false)"
END_EXPR="$(time_expr "$UNTIL" true)"
TZ_LIT="$(sql_literal "$TIMEZONE")"
BUCKET_LIT="$(sql_literal "$RESOLVED_BUCKET")"
META_NS="$(sql_literal "$NS")"
META_SINCE="$(sql_literal "$SINCE_LABEL")"
META_UNTIL="$(sql_literal "$UNTIL_LABEL")"
META_POD="$(sql_literal "$POSTGRES_POD")"
META_CTX="$(sql_literal "$KUBE_CONTEXT")"

SQL="$(substitute_sql "$SCRIPT_DIR/report.sql" \
  "$START_EXPR" "$END_EXPR" "$TZ_LIT" "$BUCKET_LIT" "$RECENT_LIMIT" \
  "$META_NS" "$META_SINCE" "$META_UNTIL" "$META_POD" "$META_CTX")"

mkdir -p "$OUTPUT_DIR"

KUBECTL_ARGS=()
[[ -n "$KUBE_CONTEXT" ]] && KUBECTL_ARGS+=(--context "$KUBE_CONTEXT")

JSON_TMP="$(mktemp)"
trap 'rm -f "$JSON_TMP"' EXIT

if ! printf '%s\n' "$SQL" | "$KUBECTL" "${KUBECTL_ARGS[@]}" exec -i -n "$NS" "$POSTGRES_POD" -- \
  bash -lc 'PGPASSWORD="$POSTGRES_PASSWORD" psql -U krakend -d krakend -v ON_ERROR_STOP=1 -t -A' \
  > "$JSON_TMP"; then
  echo "ERROR: kubectl exec / psql failed" >&2
  exit 1
fi

# psql -t -A may emit blank lines; take last non-empty line as JSON
JSON_LINE="$(grep -v '^[[:space:]]*$' "$JSON_TMP" | tail -n 1 || true)"
if [[ -z "$JSON_LINE" ]]; then
  echo "ERROR: empty response from PostgreSQL" >&2
  exit 1
fi

if [[ "$FORMAT" == "json" ]]; then
  OUTFILE="$OUTPUT_DIR/$(report_filename "$NS" "$SINCE_LABEL" "$UNTIL_LABEL" "$RESOLVED_BUCKET" "$TIMEZONE" json)"
  write_json_file "$OUTFILE" "$JSON_LINE"
  echo "Wrote $OUTFILE"
elif [[ "$FORMAT" == "summary" ]]; then
  JSON_OUT="$(mktemp)"
  write_json_file "$JSON_OUT" "$JSON_LINE"
  OUTFILE="$OUTPUT_DIR/$(report_filename "$NS" "$SINCE_LABEL" "$UNTIL_LABEL" "$RESOLVED_BUCKET" "$TIMEZONE" summary)"
  render_summary_from_json "$JSON_OUT" "$NS" > "$OUTFILE"
  rm -f "$JSON_OUT"
  echo "Wrote $OUTFILE"
else
  JSON_TMP_FILE="$(mktemp)"
  write_json_file "$JSON_TMP_FILE" "$JSON_LINE"
  OUTFILE="$OUTPUT_DIR/$(report_filename "$NS" "$SINCE_LABEL" "$UNTIL_LABEL" "$RESOLVED_BUCKET" "$TIMEZONE" html)"
  render_html_from_json "$JSON_TMP_FILE" "$OUTFILE" "$NS"
  rm -f "$JSON_TMP_FILE"
  echo "Wrote $OUTFILE"
fi
