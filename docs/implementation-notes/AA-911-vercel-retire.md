# AA-911 — Vercel retirement of the Booking admin (lane l4-aa911-vercel-retire, S272)

Source: root runbook `docs/ops/runbooks/staff-web-ecs-cutover.md` section 4 steps 2-4 (approved by the tech lead on
07/11/2026), root ADR 0019 d23 and root ADR 0006a A11 / A13. The DNS cut-over is done: booking. points at the shared
ALB and the ECS service `aa-booking-dev-admin` serves it. Root notes: `docs/implementation-notes/AA-911-vercel-retire.md`.

## Decisions

- **The public smoke and the visual run move to `deploy-admin-dev.yml`.** `vercel-deploy.yml` ran them after each
  production deploy. Now job `smoke` runs after `verify` succeeds, and `visual-production` after it (main only,
  report-only). The run verdict (`result`) now needs config, deploy, verify AND smoke, like the old Vercel verdict
  needed deploy and smoke; verify and smoke may skip only when the service runs no task (desired count 0).
- **`check_security_headers.sh` is deleted.** It existed to read headers through Vercel Deployment Protection
  (bypass header or `vercel curl`). The public smoke already compares every `next.config.ts` header on the production
  host after each deploy.
- **One client in the BFF.** `frontend/lib/cognito.ts` drops `VERCEL_ENV`, the Preview client and the
  `aa-booking-admin.vercel.app` origin; the allow-list is `booking.lumiguides.it.com` (+ localhost under `next dev`).
  The ECS image never set `VERCEL_ENV`, so this was already the path in use.
- **The API stops accepting the Preview client by default.** `DEV_BOOKING_STAFF_PREVIEW_CLIENT_ID` is empty, so a
  token of the deleted `booking-staff-preview` client is 401. The read-only cap code stays for a client set through
  the env (no authorization logic changed, only a default).
- **The Preview `X-Robots-Tag` is gone** with the preview builds.

## Changed

- Deleted `.github/workflows/vercel-deploy.yml`, `frontend/vercel.json`, `.github/scripts/check_security_headers.sh`.
- `deploy-admin-dev.yml`: header, jobs `smoke` and `visual-production`, `result` needs `smoke`.
- `visual.yml`: host allow-list = `booking.lumiguides.it.com` only; comments.
- `backend/app/auth/identity_config.py` (default empty), `app/auth/staff.py` and `app/settings.py` (docstrings).
- `frontend/lib/cognito.ts`, `frontend/next.config.ts`, `app/api/auth/signin/cognito/route.ts` (comment).
- Tests: `test_vercel_deploy_smoke.py` -> `test_admin_deploy_smoke.py` (the deploy-admin-dev wiring, the verdict script
  run with real `needs` JSON, a "Vercel is retired" guard), `test_aa911_preview_cap.py` (the retired id is refused by
  default), `test_identity_config.py`, `test_release_version.py`, `test_aa1008_h3_visual_required.py`.
- Docs: `CONTEXT.md`, `README.md`, `frontend/README.md`, `frontend/e2e/SIGNED-IN-SMOKE.md`, `.github/dependabot.yml`,
  `.github/pull_request_template.md`, comments in `ci.yml`, `release-please.yml`, `playwright.config.ts`,
  `e2e/smoke-public/login.spec.ts`.

## Tradeoffs

- The 4.5 MB body caps (`app/ops/files.py`, `app/supply/media.py`, the BFF upload route) were sized for Vercel
  functions. The ALB has no such limit, but API Gateway (10 MB) still does and the caps are product limits now; they
  stay, and the comments that blame Vercel are a follow-up.
- Keeping the dormant preview cap code avoids a resolver change in a workflow PR.

## Should know

- Merge after the AA-CIS-Infra apex PR; the Infra PR that deletes the Preview host and the `booking-staff-preview`
  client merges after this one.
- This PR touches `frontend/**`, so its merge runs `Deploy Admin Dev`: expect deploy, verify, smoke and
  visual-production green.
- Repo settings the orchestrator removes by hand: secret `VERCEL_TOKEN` (and any `VERCEL_*` secret), the ruleset
  "preview-signin locked (AA-911)", the branch `preview-signin`, the label `preview-signin`.
