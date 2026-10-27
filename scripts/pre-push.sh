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
    scripts/pre-push.sh | scripts/install-hooks.sh | .githooks/*) groups+=" hook" ;;
    # Unit tests read these files (deploy/verify scripts, workflows, release config, the Dockerfile, the OpenAPI
    # export); name or import matching never reaches them, select_unit_tests matches them by file name.
    .github/workflows/* | .github/scripts/* | .github/verify/* | .release-please-manifest.json | \
      release-please-config.json | backend/Dockerfile | docs/api/openapi.json) groups+=" unit" ;;
    # Marker (not a path): the diff deletes or renames a file, which can break a Markdown link.
    :deleted-or-renamed) groups+=" links" ;;
  esac
  case "$f" in
    *.md | docs/*) groups+=" docs" ;;
  esac
  echo "$groups"
}

# plan_checks <mode> [files...] -> one check id per line, in run order. mode: fast | full | ci
# Fast mode never plans unit-full (the whole suite with coverage, ~3 min in CI): a shared fixture or dependency change
# plans unit-changed and main() prints a warning (see unit_all_files).
plan_checks() {
  local mode="$1"; shift
  local g all=" " f
  if [[ "$mode" != "fast" ]]; then
    all=" hook py deps unit_full migrations fe trivyignore docs docs_tools "
  else
    for f in "$@"; do
      [[ -z "$f" ]] && continue
      for g in $(classify_file "$f"); do
        [[ "$all" == *" $g "* ]] || all+="$g "
      done
    done
    if [[ "$all" == *" unit_all "* || "$all" == *" migrations "* ]] && [[ "$all" != *" unit "* ]]; then all+="unit "; fi
  fi
  echo "resource-ids"
  [[ "$all" == *" hook "* ]] && echo "hook-self-test"
  [[ "$all" == *" trivyignore "* ]] && echo "trivyignore"
  if [[ "$all" == *" py "* ]]; then echo "ruff-check"; echo "ruff-format"; fi
  [[ "$all" == *" deps "* ]] && echo "pip-audit"
  if [[ "$all" == *" unit_full "* ]]; then echo "unit-full"
  elif [[ "$all" == *" unit "* ]]; then echo "unit-changed"; fi
  if [[ "$all" == *" docs "* ]]; then
    [[ "$all" == *" docs_tools "* ]] && echo "docs-tools-tests"
    echo "docs-frontmatter"; echo "docs-adr"; echo "docs-aws-multiline"; echo "docs-links"
  elif [[ "$all" == *" links "* ]]; then
    echo "docs-links"
  fi
  if [[ "$all" == *" fe "* ]]; then echo "fe-lint"; echo "fe-typecheck"; fi
  return 0
}

# unit_all_files [files...] -> the changed files that a targeted unit selection cannot cover (shared fixtures, the
# dependency set, pytest config), one per line.
unit_all_files() {
  local f
  for f in "$@"; do
    [[ "$(classify_file "$f")" == *" unit_all"* ]] && echo "$f"
  done
  return 0
}

# changed_files <repo root> <base> -> NUL-separated paths changed on HEAD since the merge base, unquoted (non-ASCII
# names stay as they are). Fails (non-zero) when git cannot compute the diff, e.g. no merge base.
changed_files() {
  git -C "$1" -c core.quotePath=false diff --name-only -z "$2...HEAD"
}

# deletes_or_renames <repo root> <base> -> exit 0 when the diff deletes or renames a file.
deletes_or_renames() {
  [[ "$(git -C "$1" diff --name-only -z --diff-filter=DR "$2...HEAD" | head -c 1 | wc -c)" -gt 0 ]]
}

# select_unit_tests <backend dir> [files...] -> test files under tests/unit (relative to backend/), one per line.
# Changed test files, tests named after a changed module (test_<stem>.py, test_<stem>_*.py), tests importing it,
# tests naming a changed file (a workflow, a script loaded by path), the migration guard when migrations/ changed,
# and the OpenAPI export test when any backend/app file changed (a stale docs/api/openapi.json fails CI Unit).
select_unit_tests() {
  local bdir="$1"; shift
  local f rel stem mod pkg leaf t base
  {
    for f in "$@"; do
      [[ -z "$f" ]] && continue
      base="$(basename "$f")"
      if [[ "$base" != "__init__.py" && -d "$bdir/tests/unit" ]]; then
        grep -lF -- "$base" "$bdir"/tests/unit/test_*.py 2>/dev/null | sed "s#^$bdir/##"
      fi
      if [[ "$f" == backend/app/* && -f "$bdir/tests/unit/test_openapi_export.py" ]]; then
        echo "tests/unit/test_openapi_export.py"
      fi
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
  local ALL="resource-ids hook-self-test trivyignore ruff-check ruff-format pip-audit unit-full docs-tools-tests $DOCS fe-lint fe-typecheck"
  expect "empty diff: resource IDs only" "resource-ids" fast
  expect "app module" "resource-ids ruff-check ruff-format unit-changed" fast backend/app/routers/cases.py
  expect "test file" "resource-ids ruff-check ruff-format unit-changed" fast backend/tests/unit/test_health.py
  expect "unit conftest -> targeted unit + warning (never the full suite in fast mode)" \
    "resource-ids ruff-check ruff-format unit-changed" fast backend/tests/unit/conftest.py
  expect "pyproject -> targeted unit + warning" "resource-ids ruff-check ruff-format unit-changed" fast backend/pyproject.toml
  expect "requirements -> pip-audit + targeted unit + warning" "resource-ids ruff-check ruff-format pip-audit unit-changed" \
    fast backend/requirements-dev.txt
  expect "migration -> migration guard only" "resource-ids unit-changed" fast migrations/026_example.sql
  expect "frontend -> lint + tsc, never build" "resource-ids fe-lint fe-typecheck" fast frontend/app/cases/page.tsx
  expect "markdown -> docs lint" "resource-ids $DOCS" fast CONTEXT.md
  expect "markdown with a space in the name" "resource-ids $DOCS" fast "docs/a b.md"
  expect "non-ASCII markdown name" "resource-ids $DOCS" fast "docs/Tài liệu.md"
  expect "docs tools -> their unit tests too" "resource-ids docs-tools-tests $DOCS" fast tools/docs/check_frontmatter.py
  expect ".trivyignore" "resource-ids trivyignore" fast .trivyignore
  expect "workflow only -> the unit tests that read it" "resource-ids unit-changed" fast .github/workflows/ci.yml
  expect "release config -> unit tests" "resource-ids unit-changed" fast .release-please-manifest.json
  expect "hook files -> hook self-test" "resource-ids hook-self-test" fast scripts/pre-push.sh .githooks/pre-push
  expect "delete or rename -> link check" "resource-ids docs-links" fast :deleted-or-renamed
  expect "mixed diff" "resource-ids ruff-check ruff-format unit-changed $DOCS fe-lint fe-typecheck" fast \
    backend/app/db.py docs/implementation-notes/AA-993.md frontend/lib/config.ts migrations/026_x.sql
  expect "--full ignores the diff" "$ALL" full
  expect "--ci never skips by diff" "$ALL" ci README.md

  local got
  got="$(unit_all_files backend/app/db.py backend/tests/conftest.py backend/requirements.txt | tr '\n' ' ' | sed 's/ $//')"
  n=$((n + 1))
  if [[ "$got" == "backend/tests/conftest.py backend/requirements.txt" ]]; then echo "ok   unit_all warning files"
  else echo "FAIL unit_all warning files"; echo "     got:  $got"; fails=$((fails + 1)); fi

  # Unit test selection on a fixture tree (no pytest run).
  local tmp; tmp="$(mktemp -d)"
  mkdir -p "$tmp/tests/unit"
  : >"$tmp/tests/unit/test_cases.py"; : >"$tmp/tests/unit/test_cases_flows.py"; : >"$tmp/tests/unit/test_migration_guard.py"
  echo "from app.supply.content import read_content" >"$tmp/tests/unit/test_supply_detail.py"
  echo "from app.supply import content" >"$tmp/tests/unit/test_supply_other.py"
  echo "import app.supply.contentx" >"$tmp/tests/unit/test_not_this.py"
  echo 'WORKFLOW = ROOT / ".github/workflows/deploy-dev.yml"' >"$tmp/tests/unit/test_deploy_wf.py"
  echo 'spec_from_file_location("x", ROOT / "scripts/export_openapi.py")' >"$tmp/tests/unit/test_openapi_export.py"
  sel() { select_unit_tests "$tmp" "$@" | tr '\n' ' ' | sed 's/ $//'; }
  check_sel() { # check_sel <name> <want> [files...]
    local name="$1" want="$2"; shift 2
    local got; got="$(sel "$@")"
    n=$((n + 1))
    if [[ "$got" == "$want" ]]; then echo "ok   $name"; else echo "FAIL $name"; echo "     want: $want"; echo "     got:  $got"; fails=$((fails + 1)); fi
  }
  local OA="tests/unit/test_openapi_export.py"
  check_sel "select: tests named after the module" "tests/unit/test_cases.py tests/unit/test_cases_flows.py $OA" backend/app/routers/cases.py
  check_sel "select: tests importing the module" "$OA tests/unit/test_supply_detail.py tests/unit/test_supply_other.py" backend/app/supply/content.py
  check_sel "select: migration guard" "tests/unit/test_migration_guard.py" migrations/026_x.sql
  check_sel "select: changed test file" "tests/unit/test_cases.py" backend/tests/unit/test_cases.py
  check_sel "select: app module without tests -> OpenAPI export test only" "$OA" backend/app/nothing_here.py
  check_sel "select: workflow named in a test" "tests/unit/test_deploy_wf.py" .github/workflows/deploy-dev.yml
  check_sel "select: script loaded by path" "$OA" backend/scripts/export_openapi.py
  check_sel "select: __init__.py is not matched by file name" "" backend/tests/__init__.py
  echo "from tests.migration_guard import check" >"$tmp/tests/unit/test_guard_rules.py"
  check_sel "select: shared test helper (by import and by name)" "tests/unit/test_guard_rules.py tests/unit/test_migration_guard.py" backend/tests/migration_guard.py
  rm -rf "$tmp"

  # The diff reader on a scratch repo: unquoted non-ASCII and spaced names, a failing diff is reported.
  tmp="$(mktemp -d)"
  local g=(git -C "$tmp" -c user.name=t -c user.email=t@example.invalid -c commit.gpgsign=false -c core.hooksPath=/dev/null)
  local files=()
  if "${g[@]}" init -q -b main 2>/dev/null && "${g[@]}" commit -q --allow-empty -m base &&
    mkdir -p "$tmp/docs" && : >"$tmp/docs/a b.md" && : >"$tmp/docs/Tài.md" && "${g[@]}" add -A &&
    "${g[@]}" commit -q -m docs; then
    mapfile -d '' -t files < <(changed_files "$tmp" main~1)
    got="$(plan_checks fast "${files[@]}" | tr '\n' ' ' | sed 's/ $//')"
    n=$((n + 1))
    if [[ ${#files[@]} -eq 2 && "$got" == "resource-ids $DOCS" ]]; then echo "ok   diff reader: non-ASCII and spaced names"
    else echo "FAIL diff reader: non-ASCII and spaced names"; echo "     files: ${files[*]}"; echo "     got:  $got"; fails=$((fails + 1)); fi
    n=$((n + 1))
    if changed_files "$tmp" no-such-ref >/dev/null 2>&1; then echo "FAIL diff reader: a bad base must fail"; fails=$((fails + 1))
    else echo "ok   diff reader: a bad base fails"; fi
    n=$((n + 1))
    "${g[@]}" mv "docs/a b.md" docs/c.md && "${g[@]}" commit -q -m mv
    if deletes_or_renames "$tmp" main~1 && ! deletes_or_renames "$tmp" main~2; then echo "ok   diff reader: deletes/renames"
    else echo "FAIL diff reader: deletes/renames"; fails=$((fails + 1)); fi
  else
    n=$((n + 1)); echo "FAIL diff reader: could not build the scratch repo"; fails=$((fails + 1))
  fi
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

# last_failed_unit_files <backend dir> -> unit test files recorded as failed in pytest's cache that still exist,
# relative to backend/, one per line. Entries outside tests/unit/ (integration runs) and stale ones are dropped.
last_failed_unit_files() {
  [[ -n "${BACKEND_PY:-}" && -s "$1/.pytest_cache/v/cache/lastfailed" ]] || return 0
  "$BACKEND_PY" - "$1" <<'PYEOF' 2>/dev/null || true
import json, os, sys
root = sys.argv[1]
try:
    with open(os.path.join(root, ".pytest_cache/v/cache/lastfailed"), encoding="utf-8") as fh:
        data = json.load(fh)
except (OSError, ValueError):
    sys.exit(0)
seen = set()
for key in data if isinstance(data, dict) else ():
    if not isinstance(key, str) or not key.startswith("tests/unit/"):
        continue
    path = key.split("::", 1)[0]
    if path.endswith(".py") and path not in seen and os.path.isfile(os.path.join(root, path)):
        seen.add(path)
        print(path)
PYEOF
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
LYCHEE_HINT="install lychee v0.24.2 (the version lychee-action v2.9.0 runs in CI): a release binary from https://github.com/lycheeverse/lychee/releases, or cargo install lychee --version 0.24.2"

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
      local want ver; want="pip-audit==2.10.1"; ver="${want#*==}"
      if [[ -z "$BACKEND_PY" ]] || ! "$BACKEND_PY" -m pip_audit --version 2>/dev/null | grep -qE "(^| )${ver//./\\.}\$"; then
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
        if ! "$BACKEND_PY" -c "import xdist, pytest_cov" >/dev/null 2>&1; then
          fail_setup "$id" "ci.yml / Unit Tests" "pytest-xdist / pytest-cov missing in $BACKEND_PY (the CI command uses -n 4 --cov)" \
            "$BACKEND_PY -m pip install -r backend/requirements-dev.txt"
          return
        fi
        run_step "$id" backend "ci.yml / Unit Tests / Run unit tests" \
          "ENVIRONMENT=test COVERAGE_CORE=sysmon pytest tests/unit -n 4 --tb=short --cov=app --cov-report=term-missing --cov-fail-under=90" -- \
          env ENVIRONMENT=test COVERAGE_CORE=sysmon "$BACKEND_PY" -m pytest tests/unit -n 4 --tb=short --cov=app \
          --cov-report=term-missing --cov-fail-under=90
        return
      fi
      local tests=()
      mapfile -t tests < <(select_unit_tests "$REPO_ROOT/backend" "${CHANGED[@]}")
      # Plus the unit test files that failed last time and still exist (pytest's cache), in the same run. Not --lf:
      # with only stale or non-unit entries in the cache, --lf deselects nothing and runs the whole suite serially.
      local last=()
      mapfile -t last < <(last_failed_unit_files "$REPO_ROOT/backend")
      if [[ ${#last[@]} -gt 0 ]]; then
        echo "    + ${#last[@]} test file(s) that failed last time"
        mapfile -t tests < <(printf '%s\n' "${tests[@]}" "${last[@]}" | sed '/^$/d' | sort -u)
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
      # check_frontmatter.py (and the tools' tests, which import it) need jsonschema + PyYAML; the ADR and AWS text
      # checks are stdlib only and run on any python3.
      local dpy=""
      if [[ "$id" == "docs-frontmatter" || "$id" == "docs-tools-tests" ]] && ! dpy="$(resolve_docs_python)"; then
        fail_setup "$id" "docs-lint.yml" "no Python with jsonschema + PyYAML" \
          "python3 -m pip install -r tools/docs/requirements.txt"
        return
      fi
      [[ -z "$dpy" ]] && dpy="$(resolve_docs_python || command -v python3)"
      if [[ -z "$dpy" ]]; then
        fail_setup "$id" "docs-lint.yml" "no python3 on PATH" "install Python 3.12, then python3 -m pip install -r tools/docs/requirements.txt"
        return
      fi
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
          fail_setup "$id" "docs-lint.yml / Internal links (offline)" "lychee not installed" "$LYCHEE_HINT"
        else
          echo; echo "==> [$id] skipped: lychee not installed (CI checks internal links)"
          WARNED+=("$id: lychee not installed; $LYCHEE_HINT")
        fi
        return
      fi
      # The tracked pages only (what the CI checkout has): a local './**/*.md' would also walk frontend/node_modules
      # and backend/.venv.
      run_step "$id" "" "docs-lint.yml / Internal links (offline)" \
        "git ls-files -z '*.md' | xargs -0 -r lychee --config tools/docs/lychee.toml" -- \
        bash -c 'git ls-files -z "*.md" | xargs -0 -r lychee --config tools/docs/lychee.toml' ;;
    hook-self-test)
      run_step "$id" "" "this hook's own decision tests (not a CI step)" \
        "bash scripts/pre-push.sh --self-test" -- bash scripts/pre-push.sh --self-test ;;
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
PLAN_INPUT=()
if [[ "$MODE" == "fast" ]]; then
  if ! git -C "$REPO_ROOT" rev-parse --verify -q "$BASE" >/dev/null; then
    echo "pre-push: $BASE not found (git fetch origin?), running every check"
    MODE="full"
  else
    # A failing diff (no merge base: shallow clone, rewritten main) must not look like an empty diff.
    diff_out="$(mktemp)"
    if changed_files "$REPO_ROOT" "$BASE" >"$diff_out"; then
      mapfile -d '' -t CHANGED <"$diff_out"
      PLAN_INPUT=("${CHANGED[@]}")
      deletes_or_renames "$REPO_ROOT" "$BASE" && PLAN_INPUT+=(":deleted-or-renamed")
      mapfile -t unit_all < <(unit_all_files "${CHANGED[@]}")
      if [[ ${#unit_all[@]} -gt 0 ]]; then
        WARNED+=("shared fixture/deps changed (${unit_all[*]}): CI runs the whole unit suite; run AA_PREPUSH=full git push to run it locally")
      fi
    else
      echo "pre-push: cannot diff $BASE...HEAD, running every check"
      MODE="full"
    fi
    rm -f "$diff_out"
  fi
fi

mapfile -t CHECKS < <(plan_checks "$MODE" "${PLAN_INPUT[@]}")
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
