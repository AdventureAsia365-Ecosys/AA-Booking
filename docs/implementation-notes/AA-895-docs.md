# AA-895 — Dependabot configuration for AA-Booking

Session S243 (19/10/2026), Claude Code. Branch `chore/aa895-dependabot`. Security baseline and vulnerability
process: AA-Ecosys `docs/security/baseline.md` and `docs/security/vulnerability-management.md`.

## Decisions

- Same model as AA-CIS-App (S242): npm (`/frontend`) and pip (`/backend`) get **security updates only**, grouped
  into one PR per ecosystem (`open-pull-requests-limit: 0` turns version updates off).
- `github-actions` gets weekly **version** updates in one grouped PR, like AA-CIS-Infra (AA-867). Pinned action
  versions are a supply-chain control and the volume is small.
- No `docker` entry: Dependabot security updates do not cover base images; the Trivy image scan belongs to AA-879
  (CI gate parity).

## Changed

- New `.github/dependabot.yml`. No workflow or code change.

## Tradeoffs

- Grouping trades one PR per advisory for one PR per week per ecosystem. The PR may carry several bumps; CI
  (Security Audit, Unit, Integration, Docker build) still runs on it.
- Dependabot PRs get no repository secrets, so the Vercel deploy check fails on them; the fix is to push the same
  change from a normal branch (S242 practice in CIS).

## Should know

- Live state read 19/10/2026 (GitHub Dependabot alerts API, open alerts): **38 open alerts**, all in
  `frontend/package-lock.json`, all created today when the alerts were first computed: 3 critical, 18 high,
  14 medium, 3 low. Packages: `next` (pinned `16.2.4` in `package.json`; every `next` alert is fixed by
  `16.3.8`), `postcss`, `sharp`, `source-map-js`. Dependabot already opened #38, #39 and #40 (individual PRs,
  before this grouping). Under the AA-Ecosys SLA a critical alert with a fix is due in 7 days: bump `next` to
  `16.3.8` (and `eslint-config-next` with it) from a normal branch, run the frontend build and UI smoke, then
  close #38-#40. That bump is not part of this PR.
- Repo settings (read 19/10/2026): Dependabot alerts on, security updates on, secret scanning and push protection
  on, ruleset "main: no force-push, no deletion (AA-867)" active, 5 required checks.
