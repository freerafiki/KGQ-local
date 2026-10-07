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

# json_hygiene LABEL FILE
# Structural leak check on a JSON response body (needs python3: grep cannot
# tell a 1024-float embedding vector from a dimension count). Fails on:
#   - *EmbeddingStatus keys (same filter get_node_detail() applies)
#   - raw numeric arrays longer than 30 elements = un-summarised vectors
#   - secret-looking keys (password/token/...) and connection strings
#     (bolt://, neo4j://, mongodb://) anywhere in the document
#   - leaked stack traces ("Traceback ...", "site-packages")
#   - vectorProps values that are not int dimension counts
# One FAIL line per violation; nothing found -> a single PASS.
json_hygiene() {
    local label=$1 file=$2 issues
    if ! command -v python3 >/dev/null 2>&1; then
        bad "$label: python3 unavailable, cannot inspect payload"
        return 0
    fi
    # NB: `if ! issues=$(...)` so a python crash cannot trip `set -e`.
    if ! issues=$(python3 - "$file" <<'PY'
import json, re, sys

try:
    with open(sys.argv[1]) as fh:
        doc = json.load(fh)
except Exception as exc:                       # unreadable or non-JSON body
    print(f"unparseable body: {exc}")
    sys.exit(0)

issues = []
URI = re.compile(r"bolt://|neo4j\+?s?://|mongodb(\+srv)?://", re.I)
SECRET_KEY = re.compile(r"^(password|passwd|secret|token|api_key|apikey|"
                        r"authorization|credentials?)$", re.I)


def walk(node, path="$"):
    if isinstance(node, dict):
        for key, val in node.items():
            if re.search(r"embeddingstatus", key, re.I):
                issues.append(f"*EmbeddingStatus key at {path}.{key}")
            if SECRET_KEY.match(key):
                issues.append(f"secret-looking key at {path}.{key}")
            walk(val, f"{path}.{key}")
    elif isinstance(node, list):
        # An embedding vector: long, all-numeric. Titles/actors etc. are
        # strings, score arrays are short — only vectors trip this.
        if len(node) > 30 and all(
                isinstance(x, (int, float)) and not isinstance(x, bool)
                for x in node):
            issues.append(f"raw float array (len {len(node)}) at {path}")
        for i, val in enumerate(node):
            walk(val, f"{path}[{i}]")
    elif isinstance(node, str):
        if URI.search(node):
            issues.append(f"connection string at {path}")
        if "Traceback (most recent call last)" in node or "site-packages" in node:
            issues.append(f"stack trace at {path}")


walk(doc)
# /node must summarise vectors as dimension counts (ints), never arrays.
vp = doc.get("vectorProps") if isinstance(doc, dict) else None
if isinstance(vp, dict):
    for key, val in vp.items():
        if isinstance(val, bool) or not isinstance(val, int):
            issues.append(f"vectorProps.{key} is {type(val).__name__}, "
                          f"want int dimension count")

print("\n".join(issues))
PY
    ); then
        bad "$label: payload inspection crashed (python3 error)"
        return 0
    fi
    if [ -z "$issues" ]; then
        ok "$label: no internal keys, raw vectors, or connection strings"
    else
        while IFS= read -r line; do
            [ -n "$line" ] && bad "$label: $line"
        done <<< "$issues"
    fi
    return 0
}

echo "target: ${API}"
echo "public origin: ${PUBLIC_ORIGIN}"
if [ "$PUBLIC_ORIGIN" = "https://example.invalid" ]; then
    # Not a FAIL (the placeholder still catches a wildcard ACAO), but the
    # "no CORS for the real origin" claim is NOT proven by this run.
    warn "PUBLIC_ORIGIN not set: the no-CORS checks below ran against the"
    warn "placeholder; re-run with PUBLIC_ORIGIN=https://<your real origin>."
fi

# ---------------------------------------------------------------- reachability
section "reachability"
request GET /health
if [ "$STATUS" = "000" ]; then
    echo "  FATAL API not reachable at ${API}" >&2
    echo "        start it: uvicorn app:app --host 127.0.0.1 --port 28000" >&2
    exit 2
fi
status_is "GET /health" 200

# ---------------------------------------------------------- server version banner
# $HDRS still holds the /health response. "server: uvicorn" (no digits) passes;
# "Server: Apache/2.4.58 (Ubuntu)" fails -> set "ServerTokens Prod" in Apache.
section "server banner carries no version number"
header_absent "server: header has no x.y version digits" '^server:.*[0-9]+\.[0-9]+'

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

# ------------------------------------------------------- error responses stay generic
# 4xx bodies must be plain FastAPI JSON, never a traceback or connection
# details (a Neo4j outage surfaces here first).
section "error responses leak no internals"
STATUS=$(curl -sS -o "$TMP/err422.json" -w '%{http_code}' --max-time "$TIMEOUT" \
             -X POST -H 'Content-Type: application/json' -d '{broken' \
             "${API}/search" 2>"$ERR" || echo 000)
status_is "POST /search with malformed JSON" 422
json_hygiene "malformed-JSON error body" "$TMP/err422.json"
STATUS=$(curl -sS -o "$TMP/err404.json" -w '%{http_code}' --max-time "$TIMEOUT" \
             "${API}/node/not-an-eid" 2>"$ERR" || echo 000)
status_is "GET /node/not-an-eid" 404
json_hygiene "unknown-node error body" "$TMP/err404.json"

# ------------------------------------------------------- payload hygiene
# One small search probe every run (not only when EID is passed in): its
# payload is checked below, and its first result's eid is what the /node
# check fetches — unless the operator pinned EID themselves.
section "payload hygiene (search + node payloads)"
PROBE_CODE=$(curl -sS -o "$TMP/search.json" -w '%{http_code}' --max-time "$TIMEOUT" \
                 -X POST -H 'Content-Type: application/json' \
                 -d '{"query":"energy efficiency","source_k":3,"final_k":1}' \
                 "${API}/search" 2>"$ERR" || echo 000)
STATUS=$PROBE_CODE
status_is "POST /search probe (source_k=3, final_k=1)" 200
if [ "$PROBE_CODE" = "200" ]; then
    json_hygiene "search payload" "$TMP/search.json"
fi

# The node to inspect: EID if given, else the probe's first hit.
CHECK_EID="${EID:-}"
if [ -z "$CHECK_EID" ] && [ "$PROBE_CODE" = "200" ]; then
    # neo4j_id doubles as the public node key (static/index.html uses it as
    # data-eid), and /node accepts it as the elementId path segment.
    CHECK_EID=$(python3 -c 'import json,sys; r=json.load(open(sys.argv[1])).get("results") or []; print(r[0].get("neo4j_id", "") if r else "")' \
                    "$TMP/search.json" 2>/dev/null || true)
fi
if [ -z "$CHECK_EID" ]; then
    bad "no node to inspect (search returned no results; pass EID=... explicitly)"
else
    NODE_CODE=$(curl -sS -o "$TMP/node.json" -w '%{http_code}' --max-time "$TIMEOUT" \
                    "${API}/node/${CHECK_EID}" 2>"$ERR" || echo 000)
    if [ "$NODE_CODE" != "200" ]; then
        bad "GET /node/<eid> -> ${NODE_CODE}, want 200 (stale eid? get one from /search)"
    else
        # NB: published-by-choice fields (expert_evaluation,
        # internal_record_state, note, submitted, ...) are NOT blocked here —
        # the frontend renders them; see git history for that decision.
        json_hygiene "node payload" "$TMP/node.json"
    fi
fi

# --------------------------------------------- public surface must stay dark
# Optional: same assertions through Apache once PUBLIC_API is set. /metrics
# is served locally on purpose (scrape it from the server after logging in),
# so it must be unreachable from outside; /docs was never proxied either.
if [ -n "${PUBLIC_API:-}" ]; then
    section "public URL surface (${PUBLIC_API}): internals not proxied"
    for path in /metrics /docs /redoc /openapi.json; do
        code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time "$TIMEOUT" \
                   "${PUBLIC_API}${path}" 2>"$ERR" || echo 000)
        if [ "$code" = "404" ]; then
            ok "GET ${path} through public URL -> 404"
        else
            bad "GET ${path} through public URL -> got $code, want 404"
        fi
    done
fi

# --------------------------------------------------------------------- verdict
printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
