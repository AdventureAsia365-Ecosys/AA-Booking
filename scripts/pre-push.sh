#!/usr/bin/env bash
# AA-Booking pre-push checks (AA-993 H1, root ADR 0016 draft D5): run the CI Lint / Unit / docs checks locally
# before a push, with the same commands and pinned tool versions as .github/workflows/ci.yml and docs-lint.yml.
#
# Usage (from anywhere inside the repo):
#   scripts/pre-push.sh              fast mode (default): only the checks the diff vs origin/main needs
#   scripts/pre-push.sh --full       every local check, the unit step = the exact CI command with coverage
#   scripts/pre-push.sh --ci         reserved for H2 (CI calls this script): like --full, never skips by diff,
#                                    and a missing optional tool (lychee) is a failure, not a warning
#   scripts/pre-push.sh --plan       print the checks it would run and exit (no tool is invoked)
#   scripts/pre-push.sh --self-test  test the decision logic on fixture diffs (no tool is invoked)
# Environment: AA_PREPUSH=full (same as --full), AA_PREPUSH_BASE=<ref> (default origin/main),
#   AA_PREPUSH_VENV=<venv dir> (Python env with backend/requirements-dev.txt installed).
# Bypass once: git push --no-verify. Uninstall: git config --unset core.hooksPath.
#
# Not run locally (CI only): Integration Tests (needs PostgreSQL), Docker Build Check / Trivy / SBOM (heavy),
# Vendored CIS copies and OpenAPI breaking changes (network, pinned binaries), gitleaks, PR title. Fast mode never
# runs `next build`.
set -uo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || { echo "pre-push: not inside a git repo" >&2; exit 2; }
BYPASS_HINT="push without the checks (once): git push --no-verify"

# ---------------------------------------------------------------------------------------------------------------
# Decision logic (pure: file list in, check ids out). --self-test exercises only this part.
# ---------------------------------------------------------------------------------------------------------------

# classify_file <path> -> space-separated groups
classify_file() {
  local f="$1" groups=""
  case "$f" in
    backend/requirements*.txt) groups+=" py deps unit_all" ;;
    backend/pyproject.toml) groups+=" py unit_all" ;;
  esac
  case "$f" in
    backend/tests/unit/conftest.py | backend/tests/unit/fakes.py | backend/tests/conftest.py) groups+=" py unit_all" ;;
    backend/*.py) groups+=" py unit" ;;
  esac
  case "$f" in
    migrations/*) groups+=" migrations" ;;
    frontend/*) groups+=" fe" ;;
    .trivyignore) groups+=" trivyignore" ;;
    tools/docs/*) groups+=" docs docs_tools" ;;
  esac
  case "$f" in
    *.md | docs/*) groups+=" docs" ;;
  esac
  echo "$groups"
}

# plan_checks <mode> [files...] -> one check id per line, in run order. mode: fast | full | ci
plan_checks() {
  local mode="$1"; shift
  local g all=" " f
  if [[ "$mode" != "fast" ]]; then
    all=" py deps unit_full migrations fe trivyignore docs docs_tools "
  else
    for f in "$@"; do
      [[ -z "$f" ]] && continue
      for g in $(classify_file "$f"); do
        [[ "$all" == *" $g "* ]] || all+="$g "
      done
    done
    # A shared fixture or the dependency set changed: the targeted selection is not enough, run the CI command.
    if [[ "$all" == *" unit_all "* ]]; then all="${all/ unit / }"; all+="unit_full "; fi
    if [[ "$all" == *" migrations "* && "$all" != *" unit_full "* && "$all" != *" unit "* ]]; then all+="unit "; fi
  fi
  echo "resource-ids"
  [[ "$all" == *" trivyignore "* ]] && echo "trivyignore"
  if [[ "$all" == *" py "* ]]; then echo "ruff-check"; echo "ruff-format"; fi
  [[ "$all" == *" deps "* ]] && echo "pip-audit"
  if [[ "$all" == *" unit_full "* ]]; then echo "unit-full"
  elif [[ "$all" == *" unit "* ]]; then echo "unit-changed"; fi
  if [[ "$all" == *" docs "* ]]; then
    [[ "$all" == *" docs_tools "* ]] && echo "docs-tools-tests"
    echo "docs-frontmatter"; echo "docs-adr"; echo "docs-aws-multiline"; echo "docs-links"
  fi
  if [[ "$all" == *" fe "* ]]; then echo "fe-lint"; echo "fe-typecheck"; fi
  return 0
}

# select_unit_tests <backend dir> [files...] -> test files under tests/unit (relative to backend/), one per line.
# Changed test files, tests named after a changed module (test_<stem>.py, test_<stem>_*.py), tests importing it,
# and the migration guard when migrations/ changed.
select_unit_tests() {
  local bdir="$1"; shift
  local f rel stem mod pkg leaf t
  {
    for f in "$@"; do
      case "$f" in
        migrations/*) [[ -f "$bdir/tests/unit/test_migration_guard.py" ]] && echo "tests/unit/test_migration_guard.py" ;;
        backend/tests/unit/test_*.py) rel="${f#backend/}"; [[ -f "$bdir/$rel" ]] && echo "$rel" ;;
        backend/*.py)
          rel="${f#backend/}"; stem="$(basename "$rel" .py)"
          mod="${rel%.py}"; mod="${mod//\//.}"; [[ "$stem" == "__init__" ]] && { mod="${mod%.__init__}"; stem="${mod##*.}"; }
          pkg="${mod%.*}"; leaf="${mod##*.}"
          for t in "$bdir"/tests/unit/test_"$stem".py "$bdir"/tests/unit/test_"$stem"_*.py; do
            [[ -f "$t" ]] && echo "tests/unit/${t##*/}"
          done
          [[ -d "$bdir/tests/unit" ]] && grep -lE "(^|[^A-Za-z0-9_.])${mod//./\\.}([^A-Za-z0-9_]|$)|from ${pkg//./\\.} import .*\b${leaf}\b" \
            "$bdir"/tests/unit/test_*.py 2>/dev/null | sed "s#^$bdir/##"
          ;;
      esac
    done
  } | sort -u
}

# ---------------------------------------------------------------------------------------------------------------
# --self-test
# ---------------------------------------------------------------------------------------------------------------
self_test() {
  local fails=0 n=0
  expect() { # expect <name> <expected ids, space-separated> <mode> [files...]
    local name="$1" want="$2" mode="$3"; shift 3
    local got; got="$(plan_checks "$mode" "$@" | tr '\n' ' ' | sed 's/ $//')"
    n=$((n + 1))
    if [[ "$got" == "$want" ]]; then echo "ok   $name"; else echo "FAIL $name"; echo "     want: $want"; echo "     got:  $got"; fails=$((fails + 1)); fi
  }
  local DOCS="docs-frontmatter docs-adr docs-aws-multiline docs-links"
  local ALL="resource-ids trivyignore ruff-check ruff-format pip-audit unit-full docs-tools-tests $DOCS fe-lint fe-typecheck"
  expect "empty diff: resource IDs only" "resource-ids" fast
  expect "app module" "resource-ids ruff-check ruff-format unit-changed" fast backend/app/routers/cases.py
  expect "test file" "resource-ids ruff-check ruff-format unit-changed" fast backend/tests/unit/test_health.py
  expect "unit conftest -> full unit command" "resource-ids ruff-check ruff-format unit-full" fast backend/tests/unit/conftest.py
  expect "pyproject -> full unit command" "resource-ids ruff-check ruff-format unit-full" fast backend/pyproject.toml
  expect "requirements -> pip-audit + full unit" "resource-ids ruff-check ruff-format pip-audit unit-full" fast backend/requirements-dev.txt
  expect "migration -> migration guard only" "resource-ids unit-changed" fast migrations/026_example.sql
  expect "frontend -> lint + tsc, never build" "resource-ids fe-lint fe-typecheck" fast frontend/app/cases/page.tsx
  expect "markdown -> docs lint" "resource-ids $DOCS" fast CONTEXT.md
  expect "docs tools -> their unit tests too" "resource-ids docs-tools-tests $DOCS" fast tools/docs/check_frontmatter.py
  expect ".trivyignore" "resource-ids trivyignore" fast .trivyignore
  expect "workflow only" "resource-ids" fast .github/workflows/ci.yml
  expect "mixed diff" "resource-ids ruff-check ruff-format unit-changed $DOCS fe-lint fe-typecheck" fast \
    backend/app/db.py docs/implementation-notes/AA-993.md frontend/lib/config.ts migrations/026_x.sql
  expect "--full ignores the diff" "$ALL" full
  expect "--ci never skips by diff" "$ALL" ci README.md

  # Unit test selection on a fixture tree (no pytest run).
  local tmp; tmp="$(mktemp -d)"
  mkdir -p "$tmp/tests/unit"
  : >"$tmp/tests/unit/test_cases.py"; : >"$tmp/tests/unit/test_cases_flows.py"; : >"$tmp/tests/unit/test_migration_guard.py"
  echo "from app.supply.content import read_content" >"$tmp/tests/unit/test_supply_detail.py"
  echo "from app.supply import content" >"$tmp/tests/unit/test_supply_other.py"
  echo "import app.supply.contentx" >"$tmp/tests/unit/test_not_this.py"
  sel() { select_unit_tests "$tmp" "$@" | tr '\n' ' ' | sed 's/ $//'; }
  check_sel() { # check_sel <name> <want> [files...]
    local name="$1" want="$2"; shift 2
    local got; got="$(sel "$@")"
    n=$((n + 1))
    if [[ "$got" == "$want" ]]; then echo "ok   $name"; else echo "FAIL $name"; echo "     want: $want"; echo "     got:  $got"; fails=$((fails + 1)); fi
  }
  check_sel "select: tests named after the module" "tests/unit/test_cases.py tests/unit/test_cases_flows.py" backend/app/routers/cases.py
  check_sel "select: tests importing the module" "tests/unit/test_supply_detail.py tests/unit/test_supply_other.py" backend/app/supply/content.py
  check_sel "select: migration guard" "tests/unit/test_migration_guard.py" migrations/026_x.sql
  check_sel "select: changed test file" "tests/unit/test_cases.py" backend/tests/unit/test_cases.py
  check_sel "select: nothing matches" "" backend/app/nothing_here.py
  echo "from tests.migration_guard import check" >"$tmp/tests/unit/test_guard_rules.py"
  check_sel "select: shared test helper (by import and by name)" "tests/unit/test_guard_rules.py tests/unit/test_migration_guard.py" backend/tests/migration_guard.py
  rm -rf "$tmp"

  echo "self-test: $((n - fails))/$n passed"
  [[ $fails -eq 0 ]]
}

# ---------------------------------------------------------------------------------------------------------------
# Tool resolution
# ---------------------------------------------------------------------------------------------------------------
pin_of() { grep -E "^$1==" "$REPO_ROOT/backend/requirements-dev.txt" | head -1; }

# The backend Python: AA_PREPUSH_VENV, this checkout's backend/.venv, the main checkout's backend/.venv (worktrees
# share it), then python3 on PATH. It must import the dev requirements (fastapi, pytest).
resolve_backend_python() {
  local main_root cand
  main_root="$(cd "$(git rev-parse --git-common-dir)/.." && pwd)"
  for cand in "${AA_PREPUSH_VENV:+$AA_PREPUSH_VENV/bin/python}" "$REPO_ROOT/backend/.venv/bin/python" \
    "$main_root/backend/.venv/bin/python" "$(command -v python3 || true)"; do
    [[ -n "$cand" && -x "$cand" ]] || continue
    if "$cand" -c "import fastapi, pytest" >/dev/null 2>&1; then echo "$cand"; return 0; fi
  done
  return 1
}

# Python for tools/docs (needs tools/docs/requirements.txt: jsonschema, PyYAML).
resolve_docs_python() {
  local cand
  for cand in "${BACKEND_PY:-}" "$(command -v python3 || true)"; do
    [[ -n "$cand" && -x "$cand" ]] || continue
    if "$cand" -c "import jsonschema, yaml" >/dev/null 2>&1; then echo "$cand"; return 0; fi
  done
  return 1
}

# ---------------------------------------------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------------------------------------------
FAILED=()
WARNED=()

# run_step <check id> <dir relative to repo root> <ci reference> <display command> -- <argv...>
run_step() {
  local id="$1" dir="$2" ci="$3" shown="$4"; shift 5
  local t0 rc
  t0=$(date +%s)
  echo
  echo "==> [$id] (cd ${dir:-.} && $shown)"
  (cd "$REPO_ROOT/$dir" && "$@")
  rc=$?
  if [[ $rc -eq 0 ]]; then
    echo "    ok ($(( $(date +%s) - t0 ))s)"
  else
    echo "    FAILED (exit $rc, $(( $(date +%s) - t0 ))s)"
    FAILED+=("$id|${dir:-.}|$shown|$ci")
  fi
}

# fail_setup <check id> <ci reference> <what is missing> <how to fix>
fail_setup() {
  echo
  echo "==> [$1] cannot run: $3"
  echo "    fix: $4"
  FAILED+=("$1|.|$4|$2")
}

run_check() {
  local id="$1"
  case "$id" in
    resource-ids)
      run_step "$id" "" "ci.yml / Security Audit / No literal AWS resource IDs" \
        "python3 .github/scripts/check_resource_ids.py" -- python3 .github/scripts/check_resource_ids.py ;;
    trivyignore)
      run_step "$id" "" "ci.yml / Docker Build Check / Trivy ignore entries" \
        "python3 .github/scripts/check_trivyignore.py .trivyignore" -- python3 .github/scripts/check_trivyignore.py .trivyignore ;;
    ruff-check | ruff-format)
      if [[ -z "$BACKEND_PY" ]]; then
        fail_setup "$id" "ci.yml / Lint" "no Python env with backend/requirements-dev.txt" \
          "python3.12 -m venv backend/.venv && backend/.venv/bin/pip install -r backend/requirements-dev.txt (or set AA_PREPUSH_VENV)"
        return
      fi
      local want have
      want="$(pin_of ruff)"; have="ruff==$("$BACKEND_PY" -m ruff --version 2>/dev/null | awk '{print $2}')"
      if [[ "$have" != "$want" ]]; then
        fail_setup "$id" "ci.yml / Lint" "ruff version $have, CI pins $want" "$BACKEND_PY -m pip install '$want'"
        return
      fi
      if [[ "$id" == "ruff-check" ]]; then
        run_step "$id" backend "ci.yml / Lint / ruff check" "ruff check ." -- "$BACKEND_PY" -m ruff check .
      else
        run_step "$id" backend "ci.yml / Lint / ruff format --check" "ruff format --check ." -- "$BACKEND_PY" -m ruff format --check .
      fi ;;
    pip-audit)
      local want; want="pip-audit==2.10.1"
      if [[ -z "$BACKEND_PY" ]] || ! "$BACKEND_PY" -m pip_audit --version 2>/dev/null | grep -q "${want#*==}"; then
        fail_setup "$id" "ci.yml / Security Audit / pip-audit" "$want not installed in the backend env" \
          "${BACKEND_PY:-python3} -m pip install $want"
        return
      fi
      run_step "$id" backend "ci.yml / Security Audit / pip-audit" \
        "pip-audit -r requirements.txt && pip-audit -r requirements-dev.txt" -- \
        bash -c '"$0" -m pip_audit -r requirements.txt && "$0" -m pip_audit -r requirements-dev.txt' "$BACKEND_PY" ;;
    unit-full | unit-changed)
      if [[ -z "$BACKEND_PY" ]]; then
        fail_setup "$id" "ci.yml / Unit Tests" "no Python env with backend/requirements-dev.txt" \
          "python3.12 -m venv backend/.venv && backend/.venv/bin/pip install -r backend/requirements-dev.txt (or set AA_PREPUSH_VENV)"
        return
      fi
      if [[ "$id" == "unit-full" ]]; then
        run_step "$id" backend "ci.yml / Unit Tests / Run unit tests" \
          "ENVIRONMENT=test COVERAGE_CORE=sysmon pytest tests/unit -n 4 --tb=short --cov=app --cov-report=term-missing --cov-fail-under=90" -- \
          env ENVIRONMENT=test COVERAGE_CORE=sysmon "$BACKEND_PY" -m pytest tests/unit -n 4 --tb=short --cov=app \
          --cov-report=term-missing --cov-fail-under=90
        return
      fi
      local tests=()
      mapfile -t tests < <(select_unit_tests "$REPO_ROOT/backend" "${CHANGED[@]}")
      local lf="$REPO_ROOT/backend/.pytest_cache/v/cache/lastfailed"
      if [[ -s "$lf" ]] && [[ "$(tr -d ' \n' <"$lf")" != "{}" ]]; then
        run_step "unit-last-failed" backend "ci.yml / Unit Tests (tests that failed last time)" \
          "ENVIRONMENT=test pytest tests/unit --lf --lfnf=none -q --tb=short" -- \
          bash -c '"$0" -m pytest tests/unit --lf --lfnf=none -q --tb=short; rc=$?; [ $rc -eq 5 ] && rc=0; exit $rc' "$BACKEND_PY"
      fi
      if [[ ${#tests[@]} -eq 0 ]]; then
        echo
        echo "==> [$id] no unit test matches the changed modules (--full runs the CI command)"
        return
      fi
      run_step "$id" backend "ci.yml / Unit Tests (changed modules only; CI runs all with coverage)" \
        "ENVIRONMENT=test pytest ${tests[*]} -q --tb=short" -- \
        env ENVIRONMENT=test "$BACKEND_PY" -m pytest "${tests[@]}" -q --tb=short ;;
    docs-tools-tests | docs-frontmatter | docs-adr | docs-aws-multiline)
      local dpy=""
      if [[ "$id" == "docs-frontmatter" ]] && ! dpy="$(resolve_docs_python)"; then
        fail_setup "$id" "docs-lint.yml / Front-matter schema" "no Python with jsonschema + PyYAML" \
          "python3 -m pip install -r tools/docs/requirements.txt"
        return
      fi
      [[ -z "$dpy" ]] && dpy="$(resolve_docs_python || command -v python3)"
      case "$id" in
        docs-tools-tests) run_step "$id" "" "docs-lint.yml / Unit tests of the lint tools" \
          "python -m unittest discover -s tools/docs -p 'test_*.py'" -- "$dpy" -m unittest discover -s tools/docs -p 'test_*.py' ;;
        docs-frontmatter) run_step "$id" "" "docs-lint.yml / Front-matter schema" \
          "python tools/docs/check_frontmatter.py" -- "$dpy" tools/docs/check_frontmatter.py ;;
        docs-adr) run_step "$id" "" "docs-lint.yml / ADR status and index" \
          "python tools/docs/check_adr_index.py docs/adr" -- "$dpy" tools/docs/check_adr_index.py docs/adr ;;
        docs-aws-multiline) run_step "$id" "" "docs-lint.yml / Single-line AWS CLI" \
          "python tools/docs/check_docs_text.py aws-multiline --all" -- "$dpy" tools/docs/check_docs_text.py aws-multiline --all ;;
      esac ;;
    docs-links)
      if ! command -v lychee >/dev/null 2>&1; then
        if [[ "$MODE" == "ci" ]]; then
          fail_setup "$id" "docs-lint.yml / Internal links (offline)" "lychee not installed" "install lychee (CI: lychee-action v2.9.0)"
        else
          echo; echo "==> [$id] skipped: lychee not installed (CI checks internal links)"
          WARNED+=("$id: lychee not installed")
        fi
        return
      fi
      run_step "$id" "" "docs-lint.yml / Internal links (offline)" \
        "lychee --config tools/docs/lychee.toml './**/*.md'" -- lychee --config tools/docs/lychee.toml './**/*.md' ;;
    fe-lint | fe-typecheck)
      if [[ ! -d "$REPO_ROOT/frontend/node_modules" ]]; then
        fail_setup "$id" "ci.yml / Frontend Build" "frontend/node_modules missing" "(cd frontend && npm ci)"
        return
      fi
      if [[ "$id" == "fe-lint" ]]; then
        run_step "$id" frontend "ci.yml / Frontend Build / Lint (eslint, no warnings allowed)" "npm run lint" -- npm run lint --silent
      else
        run_step "$id" frontend "ci.yml / Frontend Build / Typecheck (tsc --noEmit)" "npm run typecheck" -- npm run typecheck --silent
      fi ;;
    *) echo "pre-push: unknown check $id" >&2; FAILED+=("$id|.|unknown check|-") ;;
  esac
}

# ---------------------------------------------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------------------------------------------
MODE="fast"
PLAN_ONLY=0
[[ "${AA_PREPUSH:-}" == "full" ]] && MODE="full"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --full) [[ "$MODE" != "ci" ]] && MODE="full" ;;
    --ci) MODE="ci" ;;
    --plan) PLAN_ONLY=1 ;;
    --self-test) self_test; exit $? ;;
    -h | --help) sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "pre-push: unknown option $1 (see --help)" >&2; exit 2 ;;
  esac
  shift
done

BASE="${AA_PREPUSH_BASE:-origin/main}"
CHANGED=()
if [[ "$MODE" == "fast" ]]; then
  if git -C "$REPO_ROOT" rev-parse --verify -q "$BASE" >/dev/null; then
    mapfile -t CHANGED < <(git -C "$REPO_ROOT" diff --name-only "$BASE...HEAD")
  else
    echo "pre-push: $BASE not found (git fetch origin?), running every check"
    MODE="full"
  fi
fi

mapfile -t CHECKS < <(plan_checks "$MODE" "${CHANGED[@]}")
echo "pre-push ($MODE mode): ${#CHANGED[@]} file(s) changed vs $BASE; checks: ${CHECKS[*]}"
[[ $PLAN_ONLY -eq 1 ]] && exit 0
if [[ "$MODE" == "fast" && -n "$(git -C "$REPO_ROOT" status --porcelain --untracked-files=no)" ]]; then
  echo "note: the working tree has uncommitted changes; the checks read the working tree, the push sends commits"
fi

BACKEND_PY="$(resolve_backend_python || true)"
T_START=$(date +%s)
for c in "${CHECKS[@]}"; do run_check "$c"; done
ELAPSED=$(( $(date +%s) - T_START ))

echo
for w in "${WARNED[@]}"; do echo "warning: $w"; done
if [[ ${#FAILED[@]} -eq 0 ]]; then
  echo "pre-push: all ${#CHECKS[@]} check(s) passed in ${ELAPSED}s ($MODE mode)"
  exit 0
fi
echo "pre-push: ${#FAILED[@]} check(s) FAILED in ${ELAPSED}s ($MODE mode). Re-run the failing command to see it:"
for f in "${FAILED[@]}"; do
  IFS='|' read -r id dir shown ci <<<"$f"
  echo "  - [$id] CI: $ci"
  echo "      (cd $dir && $shown)"
done
echo "$BYPASS_HINT"
exit 1
