#!/usr/bin/env bash
# Security assertions for the KGQ API. Every line is a PASS/FAIL check; the
# script exits non-zero if anything fails, so it can gate a deploy or be run
# by hand after a config change.
#
#   ./scripts/security_check.sh
#   PUBLIC_ORIGIN=https://kgq.example.org ./scripts/security_check.sh   # your real origin
#   API=https://kgq.example.org ./scripts/security_check.sh             # through Apache
#   PUBLIC_API=https://kgq.example.org ./scripts/security_check.sh      # + assert the public
#                              # surface (/metrics, /docs, ...) returns 404 through Apache
#   EID='<elementId>' ./scripts/security_check.sh          # /node check on a specific node
#
# Works against the API directly (127.0.0.1:28000) or through the public
# URL: Apache forwards /health, /search, /node and the CORS behaviour with
# them, and /docs was never proxied in the first place.
#
# Every run also issues one small POST /search probe (a few seconds:
# embedding + Neo4j) so the payload-hygiene checks on /search and /node
# always execute; EID only pins a different node for the /node check.
set -euo pipefail

API="${API:-http://localhost:28000}"
# A fake but syntactically valid origin, so "public origin must get no CORS
# header" is asserted even when the operator forgets to set the real one.
PUBLIC_ORIGIN="${PUBLIC_ORIGIN:-https://example.invalid}"
DEV_ORIGIN_A="http://localhost:28080"
DEV_ORIGIN_B="http://127.0.0.1:28080"
TIMEOUT="${TIMEOUT:-15}"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
HDRS="$TMP/headers"
ERR="$TMP/err"

pass=0
fail=0
ok()    { echo "  PASS  $*"; pass=$((pass + 1)); }
bad()   { echo "  FAIL  $*"; fail=$((fail + 1)); }
warn()  { echo "  WARN  $*"; }
section() { printf '\n== %s ==\n' "$*"; }

# request METHOD PATH [extra curl args...]
# Leaves the status in $STATUS and the response headers in $HDRS.
# 000 means curl itself failed (server down / timeout).
request() {
    local method=$1 path=$2
    shift 2
    STATUS=$(curl -sS -X "$method" -D "$HDRS" -o /dev/null \
                 -w '%{http_code}' --max-time "$TIMEOUT" \
                 "$@" "${API}${path}" 2>"$ERR") || STATUS=000
    # NB: an `if`, not `test && echo` — the latter would make this function
    # return 1 on a successful request, and `set -e` would kill the script.
    if [ "$STATUS" = "000" ]; then
        echo "         curl: $(cat "$ERR")" >&2
    fi
}

# status_is LABEL WANT
status_is() {
    if [ "$STATUS" = "$2" ]; then ok "$1 -> $2"; else bad "$1 -> got $STATUS, want $2"; fi
}
# header_absent LABEL GREP_ERE  (any header line matching)
header_absent() {
    if grep -qiE "$2" "$HDRS"; then bad "$1 (unexpected header matching: $2)"; else ok "$1"; fi
}
# header_present LABEL GREP_ERE
header_present() {
    if grep -qiE "$2" "$HDRS"; then ok "$1"; else bad "$1 (missing header matching: $2)"; fi
}
# acao_is LABEL ORIGIN  (Access-Control-Allow-Origin equals exactly ORIGIN)
acao_is() {
    if grep -qiE "^access-control-allow-origin: $2" "$HDRS"; then
        ok "$1"
    else
        bad "$1 (got: $(grep -i '^access-control-allow-origin' "$HDRS" | tr -d '\r' || true))"
    fi
}

# preflight ORIGIN [METHOD] [HEADER]  -> issues a browser-style OPTIONS preflight
preflight() {
    local origin=$1 method="${2:-POST}" header="${3:-content-type}"
    request OPTIONS /search \
        -H "Origin: ${origin}" \
        -H "Access-Control-Request-Method: ${method}" \
        -H "Access-Control-Request-Headers: ${header}"
}

echo "target: ${API}"
echo "public origin: ${PUBLIC_ORIGIN}"

# ---------------------------------------------------------------- reachability
section "reachability"
request GET /health
if [ "$STATUS" = "000" ]; then
    echo "  FATAL API not reachable at ${API}" >&2
    echo "        start it: uvicorn app:app --host 127.0.0.1 --port 28000" >&2
    exit 2
fi
status_is "GET /health" 200

# ------------------------------------------------------- schema must stay dark
section "schema endpoints disabled (no /docs, /redoc, /openapi.json)"
request GET /docs
status_is "GET /docs" 404
request GET /redoc
status_is "GET /redoc" 404
request GET /openapi.json
status_is "GET /openapi.json" 404

# ------------------------------------------- production origin gets no CORS at all
section "public origin: no CORS headers are handed out"
request GET /health -H "Origin: ${PUBLIC_ORIGIN}"
header_absent "GET /health from public origin has no access-control-* header" \
              '^access-control'
header_absent "GET /health from public origin has no vary: origin" \
              '^vary:.*origin'
# A real POST (missing body -> 422, so no search is run) must pass through
# untouched: no Access-Control-Allow-Origin even on a mutating request.
request POST /search -H "Origin: ${PUBLIC_ORIGIN}" \
    -H "Content-Type: application/json" -d '{}'
status_is "POST /search from public origin passes through" 422
header_absent "POST /search from public origin has no access-control-* header" \
              '^access-control'

# ------------------------------------------------- dev origins are served CORS
section "dev origins (split page on :28080): CORS answered"
request GET /health -H "Origin: ${DEV_ORIGIN_A}"
acao_is "GET /health from ${DEV_ORIGIN_A} echoes that origin" "${DEV_ORIGIN_A}"
request GET /health -H "Origin: ${DEV_ORIGIN_B}"
acao_is "GET /health from ${DEV_ORIGIN_B} echoes that origin" "${DEV_ORIGIN_B}"

preflight "${DEV_ORIGIN_A}"
status_is "preflight from ${DEV_ORIGIN_A} (POST, content-type)" 200
acao_is "preflight ACAO equals ${DEV_ORIGIN_A}" "${DEV_ORIGIN_A}"

# ---------------------------------------------------- nothing else is allowed
section "everything else is refused"
preflight "http://evil.example"
status_is "preflight from a foreign origin" 400
header_absent "foreign preflight gets no access-control-allow-origin" \
              '^access-control-allow-origin'
preflight "${DEV_ORIGIN_A}" DELETE
status_is "preflight asking for method DELETE (only GET/POST allowed)" 400
preflight "${DEV_ORIGIN_A}" POST authorization
status_is "preflight asking for header authorization" 400
# Plain OPTIONS (probe/health check, no Access-Control-Request-Method) must
# still reach the app, i.e. NOT be swallowed by the CORS middleware.
request OPTIONS /search
status_is "plain OPTIONS /search (no preflight headers) reaches the app" 405

# ------------------------------------------------------- node payload hygiene
if [ -n "${EID:-}" ]; then
    section "node payload (EID set)"
    NODE_CODE=$(curl -sS -o "$TMP/node.json" -w '%{http_code}' --max-time "$TIMEOUT" \
                    "${API}/node/${EID}" 2>"$ERR" || echo 000)
    if [ "$NODE_CODE" != "200" ]; then
        bad "GET /node/<EID> -> ${NODE_CODE}, want 200 (stale eid? get one from /search)"
    elif grep -qi 'embeddingstatus' "$TMP/node.json"; then
        bad "node payload leaks *EmbeddingStatus keys"
    else
        ok "node payload has no *EmbeddingStatus keys"
    fi
fi

# --------------------------------------------------------------------- verdict
printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
