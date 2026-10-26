#!/usr/bin/env bash
# AA-993 V2 (ADR 0016 D3) — post-deploy verify of one ECS api service, run by .github/workflows/post-deploy-verify.yml
# after every Deploy Dev (and on dispatch). Red/green, no warnings: every check below must pass or the script exits 1.
# The same file (byte for byte) runs in AA-Booking (app `booking`) and AA-Partner (app `crm`).
#
# Usage: post_deploy_verify.sh <app>     (<app> names the rows file .github/verify/<app>.rows)
#
# Checks (each gets VERIFY_TRIES tries, VERIFY_RETRY_DELAY seconds apart):
#   inputs        every required input is set and well formed
#   ecs-rollout   service ACTIVE, desired >= 1 (0 is red, no scale-up), running = desired, one deployment,
#                 PRIMARY rolloutState COMPLETED
#   ecs-image     the running task definition's image = EXPECTED_IMAGE (<registry>/<repo>:<sha>, what the build pushes)
#   HTTP rows     from ROWS_FILE (.github/verify/<app>.rows): name|auth|METHOD path|expected HTTP|jq assertion
#
# Env: EXPECTED_SHA, EXPECTED_VERSION, REQUIRED_SCHEMA (<prefix>-NNN), EXPECTED_IMAGE, ECS_CLUSTER, ECS_SERVICE,
#      AWS_REGION, BASE; optional ROWS_FILE, VERIFY_TRIES (3), VERIFY_RETRY_DELAY (10), VERIFY_INITIAL_WAIT (10),
#      GITHUB_STEP_SUMMARY. Needs bash, curl, jq, aws.
# Public repo: never print the registry (only the image tag).
# Exit 0 = every check passed, 1 = at least one failed (2 = called without an app).
set -uo pipefail

APP="${1:-}"
if ! [[ "$APP" =~ ^[a-z][a-z0-9-]*$ ]]; then
  echo "::error::usage: post_deploy_verify.sh <app> (got '${APP}')"
  exit 2
fi
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROWS_FILE="${ROWS_FILE:-${HERE}/../verify/${APP}.rows}"
TRIES="${VERIFY_TRIES:-3}"
DELAY="${VERIFY_RETRY_DELAY:-10}"
INITIAL_WAIT="${VERIFY_INITIAL_WAIT:-10}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

ROWS=()   # "name<TAB>PASS|FAIL<TAB>detail"
FAILED=0
DETAIL=""

record() {  # name, PASS|FAIL, detail
  ROWS+=("$1"$'\t'"$2"$'\t'"$3")
  if [ "$2" = "PASS" ]; then
    echo "verify [$1] OK: $3"
  else
    echo "::error::verify [$1] $3"
    FAILED=1
  fi
}

retry() {  # name, function, args... — the function sets DETAIL and returns 0 (pass) / 1 (fail)
  local name="$1"; shift
  local try
  for try in $(seq 1 "$TRIES"); do
    DETAIL=""
    if "$@"; then
      record "$name" PASS "$DETAIL"
      return 0
    fi
    [ "$try" -lt "$TRIES" ] && { echo "verify [$name] try $try/$TRIES failed: $DETAIL"; sleep "$DELAY"; }
  done
  record "$name" FAIL "$DETAIL (after $TRIES tries)"
  return 1
}

tag_of() { printf '%s' "${1##*:}"; }  # the image tag only: the registry names the AWS account

# ── inputs ────────────────────────────────────────────────────────────────────────────────────────────────────────
check_inputs() {  # sets BLOCKED=1 when the ECS/HTTP checks cannot run at all
  local missing=() name
  for name in EXPECTED_SHA EXPECTED_VERSION REQUIRED_SCHEMA EXPECTED_IMAGE ECS_CLUSTER ECS_SERVICE AWS_REGION BASE; do
    [ -n "${!name:-}" ] || missing+=("$name")
  done
  if [ "${#missing[@]}" -gt 0 ]; then
    record inputs FAIL "not set: ${missing[*]}"
    BLOCKED=1
  else
    record inputs PASS "commit, version, schema, image, ECS names and base URL set"
  fi
  if [ -n "${EXPECTED_SHA:-}" ] && ! [[ "$EXPECTED_SHA" =~ ^[0-9a-f]{40}$ ]]; then
    record expected-sha FAIL "EXPECTED_SHA must be a full 40-character commit SHA, got '${EXPECTED_SHA}'"
    BLOCKED=1
  fi
  if [ -n "${REQUIRED_SCHEMA:-}" ] && ! [[ "$REQUIRED_SCHEMA" =~ ^[a-z]+-[0-9]+$ ]]; then
    record required-schema FAIL "REQUIRED_SCHEMA must look like <prefix>-NNN, got '${REQUIRED_SCHEMA}'"
    BLOCKED=1
  fi
  if [ -n "${EXPECTED_IMAGE:-}" ] && [ -n "${EXPECTED_SHA:-}" ] \
     && [ "$(tag_of "$EXPECTED_IMAGE")" != "${EXPECTED_SHA}" ]; then
    record expected-image FAIL "EXPECTED_IMAGE tag $(tag_of "$EXPECTED_IMAGE") is not ${EXPECTED_SHA}"
    BLOCKED=1
  fi
}

# ── ECS ───────────────────────────────────────────────────────────────────────────────────────────────────────────
describe_service() {  # -> $WORK/svc.json ({"services":[...]})
  aws ecs describe-services --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE" --region "$AWS_REGION" --output json \
    > "$WORK/svc.json" 2> "$WORK/svc.err"
}

check_rollout() {
  local f="$WORK/svc.json" status desired running ndeps rollout
  if ! describe_service; then
    DETAIL="describe-services failed: $(head -c 200 "$WORK/svc.err")"; return 1
  fi
  status=$(jq -r '.services[0].status // "MISSING"' "$f")
  if [ "$status" != "ACTIVE" ]; then DETAIL="service $ECS_SERVICE is $status"; return 1; fi
  desired=$(jq -r '.services[0].desiredCount // 0' "$f")
  running=$(jq -r '.services[0].runningCount // 0' "$f")
  ndeps=$(jq -r '.services[0].deployments // [] | length' "$f")
  rollout=$(jq -r '[.services[0].deployments // [] | .[] | select(.status == "PRIMARY")][0].rolloutState // "none"' "$f")
  DETAIL="desired=$desired running=$running deployments=$ndeps rolloutState=$rollout"
  if [ "$desired" -lt 1 ]; then
    DETAIL="$DETAIL — desired count 0 (scaling Dev up is a separate approved step)"; return 1
  fi
  [ "$running" = "$desired" ] && [ "$ndeps" = "1" ] && [ "$rollout" = "COMPLETED" ]
}

check_image() {
  local td img
  describe_service || { DETAIL="describe-services failed"; return 1; }
  td=$(jq -r '.services[0].taskDefinition // empty' "$WORK/svc.json" 2>/dev/null)
  if [ -z "$td" ]; then DETAIL="service $ECS_SERVICE has no task definition"; return 1; fi
  img=$(aws ecs describe-task-definition --task-definition "$td" --region "$AWS_REGION" --output json 2>/dev/null \
          | jq -r '.taskDefinition.containerDefinitions[0].image // empty')
  if [ -z "$img" ]; then DETAIL="no image in the running task definition"; return 1; fi
  DETAIL="running tag $(tag_of "$img"), built tag $(tag_of "$EXPECTED_IMAGE")"
  [ "$img" = "$EXPECTED_IMAGE" ]
}

# ── HTTP ──────────────────────────────────────────────────────────────────────────────────────────────────────────
check_row() {  # auth, method, path, expected http, jq assertion
  local auth="$1" method="$2" path="$3" want="$4" expr="$5" out="$WORK/body.json" code
  if [ "$method" != "GET" ]; then DETAIL="only GET rows are allowed, got $method"; return 1; fi
  if [ "$auth" != "public" ]; then DETAIL="auth must be public, got '$auth'"; return 1; fi
  rm -f "$out"
  code=$(curl -s --max-time 25 -o "$out" -w '%{http_code}' "${BASE%/}${path}" 2>/dev/null || true)
  DETAIL="GET $path -> HTTP ${code:-none}"
  if [ "$code" != "$want" ]; then
    DETAIL="$DETAIL, expected $want"
    [ -s "$out" ] && DETAIL="$DETAIL, body $(head -c 200 "$out" | tr -d '\n')"
    return 1
  fi
  [ -n "$expr" ] || return 0
  if ! jq -e --arg sha "$EXPECTED_SHA" --arg version "$EXPECTED_VERSION" --arg required "$REQUIRED_SCHEMA" \
       "$expr" "$out" >/dev/null 2>&1; then
    DETAIL="$DETAIL but the assertion is false: $expr; body $(head -c 300 "$out" | tr -d '\n')"
    return 1
  fi
  DETAIL="$DETAIL, assertion true"
}

run_rows() {
  local line name auth req want expr method path count=0
  if [ ! -f "$ROWS_FILE" ]; then record rows FAIL "rows file not found: $ROWS_FILE"; return; fi
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|'#'*) continue ;; esac
    IFS='|' read -r name auth req want expr <<< "$line"
    method="${req%% *}"; path="${req#* }"
    count=$((count + 1))
    retry "$name" check_row "$auth" "$method" "$path" "$want" "$expr"
  done < "$ROWS_FILE"
  [ "$count" -gt 0 ] || record rows FAIL "no rows in $ROWS_FILE"
}

# ── summary ───────────────────────────────────────────────────────────────────────────────────────────────────────
write_summary() {
  local out="${GITHUB_STEP_SUMMARY:-/dev/stdout}" verdict="PASS" row name status detail mark
  [ "$FAILED" = "0" ] || verdict="FAIL"
  {
    echo "### ${APP} post-deploy verify: ${verdict}"
    echo
    echo "Expected commit \`${EXPECTED_SHA:-unset}\`, version \`${EXPECTED_VERSION:-unset}\`," \
         "required schema \`${REQUIRED_SCHEMA:-unset}\`, image tag \`$(tag_of "${EXPECTED_IMAGE:-unset}")\`."
    echo
    echo "| Check | Result | Detail |"
    echo "|---|---|---|"
    for row in "${ROWS[@]}"; do
      IFS=$'\t' read -r name status detail <<< "$row"
      mark="PASS"; [ "$status" = "PASS" ] || mark="**FAIL**"
      echo "| ${name} | ${mark} | ${detail//|/\\|} |"
    done
  } >> "$out"
}

# ── main ──────────────────────────────────────────────────────────────────────────────────────────────────────────
BLOCKED=0
for tool in curl jq aws; do
  command -v "$tool" >/dev/null 2>&1 || { record "tool-$tool" FAIL "$tool is not installed"; BLOCKED=1; }
done
check_inputs

if [ "$BLOCKED" = "0" ]; then
  [ "$INITIAL_WAIT" -gt 0 ] && sleep "$INITIAL_WAIT"
  retry ecs-rollout check_rollout
  retry ecs-image check_image
  run_rows
else
  # The inputs are wrong: nothing below can be checked, and the table says so.
  record checks FAIL "not run: fix the inputs above"
fi

write_summary
if [ "$FAILED" = "0" ]; then
  echo "All post-deploy checks passed."
  exit 0
fi
echo "Post-deploy verify FAILED (see the table in the job summary)."
exit 1
