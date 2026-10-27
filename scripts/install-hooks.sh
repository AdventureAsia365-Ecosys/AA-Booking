#!/usr/bin/env bash
# Install the AA-Booking git hooks (AA-993 H1): point git at the tracked .githooks/ folder. One setting per clone;
# every worktree of the clone shares it. Undo: git config --unset core.hooksPath
set -euo pipefail
root="$(git rev-parse --show-toplevel)"
chmod +x "$root/.githooks/pre-push" "$root/scripts/pre-push.sh"

# core.hooksPath replaces the hooks folder: say what stops running.
old="$(git -C "$root" config --get core.hooksPath || true)"
if [[ -n "$old" && "$old" != ".githooks" ]]; then
  echo "warning: core.hooksPath was '$old'; it is replaced by .githooks (restore: git config core.hooksPath '$old')"
fi
hooks_dir="$(git -C "$root" rev-parse --path-format=absolute --git-common-dir)/hooks"
active=()
if [[ -d "$hooks_dir" ]]; then
  for h in "$hooks_dir"/*; do
    [[ -f "$h" && "$h" != *.sample ]] && active+=("${h##*/}")
  done
fi
if [[ ${#active[@]} -gt 0 ]]; then
  echo "warning: these hooks in $hooks_dir stop running once core.hooksPath is set: ${active[*]}"
  echo "         to keep one, copy it into .githooks/ (local only, do not commit it)"
fi

git -C "$root" config core.hooksPath .githooks
echo "core.hooksPath = .githooks: every git push now runs scripts/pre-push.sh (fast mode)."
echo "Full mode: AA_PREPUSH=full git push. Skip once: git push --no-verify. Undo: git config --unset core.hooksPath"
