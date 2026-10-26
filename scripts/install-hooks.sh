#!/usr/bin/env bash
# Install the AA-Booking git hooks (AA-993 H1): point git at the tracked .githooks/ folder. One setting per clone;
# every worktree of the clone shares it. Undo: git config --unset core.hooksPath
set -euo pipefail
root="$(git rev-parse --show-toplevel)"
chmod +x "$root/.githooks/pre-push" "$root/scripts/pre-push.sh"
git -C "$root" config core.hooksPath .githooks
echo "core.hooksPath = .githooks: every git push now runs scripts/pre-push.sh (fast mode)."
echo "Full mode: AA_PREPUSH=full git push. Skip once: git push --no-verify. Undo: git config --unset core.hooksPath"
