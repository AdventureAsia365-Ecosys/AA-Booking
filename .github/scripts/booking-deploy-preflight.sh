#!/usr/bin/env bash
# Booking Deploy Dev job guard (AA-751 B4), run by the `preflight` job of .github/workflows/deploy-dev.yml.
# Tested by backend/tests/unit/test_deploy_preflight_script.py with stub `aws` and `curl` on PATH.
#
# Exit 0 = the deploy may go on; exit 1 = blocked. Since AA-993 G5 (design D1.4) the gate's safe_to_deploy is
# false only while a deploy-blocking kind (the retention runs) is RUNNING; other queued/running jobs are released on
# shutdown and re-claimed by the new task. Every path that cannot read the gate blocks (fail closed), unless OVERRIDE
# carries a non-blank reason (workflow_dispatch input, Nghiệp only): then a ::warning:: names the reason and the
# deploy goes on.
#
# Environment:
#   ECS_CLUSTER, ECS_SERVICE   the Booking service (AWS credentials already configured)
#   BASE_URL                   Booking API base URL; the gate is <BASE_URL>/booking/ops/deploy-gate
#   OVERRIDE                   optional override reason (blank/whitespace = no override)
#   GATE_AUDIENCE              OIDC audience, default aa-booking-deploy-gate (app/auth/ci.py DEPLOY_GATE_AUDIENCE)
#   ACTIONS_ID_TOKEN_REQUEST_URL / _TOKEN   set by the runner when the job has `id-token: write`
set -uo pipefail

GATE_AUDIENCE="${GATE_AUDIENCE:-aa-booking-deploy-gate}"
OVERRIDE="$(printf '%s' "${OVERRIDE:-}" | tr '\r\n' '  ' | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# Blocks the deploy, or lets it through with a warning when an override reason was given.
block() {
  if [ -n "$OVERRIDE" ]; then
    echo "::warning::Deploy guard OVERRIDDEN ($1). Reason: ${OVERRIDE}"
    exit 0
  fi
  echo "::error::Deploy blocked: $1. Wait for the deploy-blocking job to finish, or re-run via workflow_dispatch with an 'override_running_jobs' reason (Nghiệp only)."
  exit 1
}

# 1. Task counts. Skip the gate only when no task runs, none is starting and none is wanted: a PENDING task (crash
#    replacement, a fresh 0 -> 1 scale) reaches RUNNING during the build, claims queued jobs, and update-service then
#    replaces it mid-job.
COUNTS=$(aws ecs describe-services --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE" \
  --query 'services[0].[runningCount,pendingCount,desiredCount]' --output text 2>"$WORK/describe.err") || COUNTS=""
read -r RUNNING PENDING DESIRED _ <<<"$COUNTS"
echo "service $ECS_SERVICE running/pending/desired: ${RUNNING:-?}/${PENDING:-?}/${DESIRED:-?}"
for n in "$RUNNING" "$PENDING" "$DESIRED"; do
  if ! [[ "$n" =~ ^[0-9]+$ ]]; then
    cat "$WORK/describe.err" >&2 || true
    block "cannot read the task counts of $ECS_SERVICE"
  fi
done
if [ "$RUNNING" = "0" ] && [ "$PENDING" = "0" ] && [ "$DESIRED" = "0" ]; then
  echo "::notice::no task running, pending or desired — nothing can be interrupted, gate skipped"
  exit 0
fi
if [ "$RUNNING" = "0" ]; then
  block "no task is RUNNING yet but $PENDING is pending (desired $DESIRED): a starting task would claim queued jobs and then be replaced — wait until it runs and re-run"
fi

# 2. This run's GitHub OIDC token (audience = the gate).
if [ -z "${ACTIONS_ID_TOKEN_REQUEST_URL:-}" ] || [ -z "${ACTIONS_ID_TOKEN_REQUEST_TOKEN:-}" ]; then
  block "no GitHub OIDC token available to this job (id-token: write missing?)"
fi
TOKEN=$(curl -sS --fail --max-time 20 -H "Authorization: bearer ${ACTIONS_ID_TOKEN_REQUEST_TOKEN}" \
  "${ACTIONS_ID_TOKEN_REQUEST_URL}&audience=${GATE_AUDIENCE}" | jq -r '.value // empty') || TOKEN=""
if [ -z "$TOKEN" ]; then
  block "could not mint the GitHub OIDC token for the deploy gate"
fi
echo "::add-mask::${TOKEN}"

# 3. The gate. Anything but HTTP 200 with a boolean safe_to_deploy and a numeric active blocks.
GATE_URL="${BASE_URL%/}/booking/ops/deploy-gate"
CODE=$(curl -sS --max-time 20 -o "$WORK/gate.json" -w "%{http_code}" \
  -H "Authorization: Bearer ${TOKEN}" "$GATE_URL") || CODE="000"
echo "GET $GATE_URL -> $CODE"
head -c 2000 "$WORK/gate.json" 2>/dev/null || true
echo
if [ "$CODE" != "200" ] || ! jq -e '(.safe_to_deploy | type) == "boolean" and (.active | type) == "number"' "$WORK/gate.json" >/dev/null 2>&1; then
  block "deploy gate unreachable or invalid (HTTP $CODE) while $RUNNING task(s) run — cannot tell whether jobs are active"
fi

ACTIVE=$(jq -r '.active' "$WORK/gate.json")
BY_KIND=$(jq -r '.by_kind // {} | to_entries | map("\(.key)=\(.value)") | join(", ")' "$WORK/gate.json")
if [ "$(jq -r '.safe_to_deploy' "$WORK/gate.json")" = "true" ]; then
  echo "No deploy-blocking job running — safe to deploy. ${ACTIVE} queued/running booking.job row(s) are released and re-claimed by the new task${BY_KIND:+ (by kind: ${BY_KIND})} (exempt: $(jq -r '.exempt_kinds // [] | join(", ")' "$WORK/gate.json"))."
  exit 0
fi
REASONS=$(jq -r '(.blocked_by // []) | join("; ")' "$WORK/gate.json")
block "${REASONS:-a deploy-blocking job is running} (all active: ${ACTIVE}${BY_KIND:+; by kind: ${BY_KIND}})"
