#!/usr/bin/env bash
# AA-879 — fail when a deployed page lacks the six security headers of frontend/next.config.ts
# (same list as the CIS admin, the CIS portal and TripPlanner, AA-876 / AA-885).
#
# Usage: check_security_headers.sh <deployment base URL> [path]   (path defaults to /login)
#
# How the page is fetched (Preview deployments sit behind Vercel Deployment Protection):
#   - BYPASS set        → curl with the x-vercel-protection-bypass header;
#   - else VERCEL_TOKEN → `vercel curl` (CLI pinned by VERCEL_CLI; team from VERCEL_ORG_ID), which
#                         reads the project's own protection-bypass token through the API with the
#                         team token; never add --debug: it prints that token in the (public) log;
#   - else              → plain curl (public hosts only).
# 3 tries, 10 s apart. Response headers only; nothing secret is printed.
set -uo pipefail
BASE="${1:?deployment base URL}"
PATH_PART="${2:-/login}"
VERCEL_CLI="${VERCEL_CLI:-vercel@60.0.1}"

fetch_headers() {
  local out="$1"
  if [ -n "${BYPASS:-}" ]; then
    curl -sS -o /dev/null -D "$out" --max-time 30 -H "x-vercel-protection-bypass: ${BYPASS}" "${BASE}${PATH_PART}"
  elif [ -n "${VERCEL_TOKEN:-}" ]; then
    # `vercel curl` forwards unknown flags (even --token / --scope) to curl, so the CLI reads the
    # token and team from the environment (VERCEL_TOKEN, VERCEL_ORG_ID) instead.
    VERCEL_TOKEN="$VERCEL_TOKEN" npx --yes "$VERCEL_CLI" curl "$PATH_PART" --deployment "$BASE" --yes \
      -- -sS -o /dev/null -D "$out" --max-time 30 >/dev/null 2>"$out.err"
  else
    curl -sS -o /dev/null -D "$out" --max-time 30 "${BASE}${PATH_PART}"
  fi
}

TMP="$(mktemp)"
MISSING="(not fetched)"
for attempt in 1 2 3; do
  : >"$TMP"
  fetch_headers "$TMP" || true
  HEADERS="$(tr -d '\r' <"$TMP")"
  STATUS="$(head -n1 <<<"$HEADERS")"
  MISSING=""
  grep -qiE "^content-security-policy:.*frame-ancestors 'none'" <<<"$HEADERS" || MISSING="$MISSING csp-frame-ancestors"
  grep -qiE '^x-content-type-options: *nosniff' <<<"$HEADERS" || MISSING="$MISSING nosniff"
  grep -qiE '^x-frame-options: *DENY' <<<"$HEADERS" || MISSING="$MISSING x-frame-options"
  grep -qiE '^referrer-policy: *strict-origin-when-cross-origin' <<<"$HEADERS" || MISSING="$MISSING referrer-policy"
  grep -qiE '^strict-transport-security:.*max-age=63072000' <<<"$HEADERS" || MISSING="$MISSING hsts"
  grep -qiE '^permissions-policy:' <<<"$HEADERS" || MISSING="$MISSING permissions-policy"
  echo "attempt $attempt: GET ${PATH_PART} -> ${STATUS:-no response}"
  [ -z "$MISSING" ] && break
  [ "$attempt" -lt 3 ] && sleep 10
done
rm -f "$TMP" "$TMP.err"

if [ -n "$MISSING" ]; then
  echo "::error::${BASE}${PATH_PART} is missing security headers:${MISSING} (a Vercel login page here means the protection bypass did not work)"
  exit 1
fi
echo "${BASE}${PATH_PART} carries all six security headers."
