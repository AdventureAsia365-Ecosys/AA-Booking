# Signed-in smoke of the staff admin (AA-910 design; built for AA-1073 / AA-1074) — agent-run, no CI job yet

Status: **built, agent-run** (AA-1073 / AA-1074, S277). The specs live in `signed-in/`: the setup project
`signed-in-setup` (`signed-in/auth.setup.ts`) asks for one code and saves the session to `e2e/results/.auth/`
(gitignored), and the project `signed-in` walks the staff pages read-only. No workflow runs it yet (last section).

```
cd frontend
AWS_PROFILE=aa365-admin AWS_REGION=us-west-1 npm run test:signed-in
# rerun without a new code while the saved session is valid:
BOOKING_E2E_REUSE_STATE=1 AWS_PROFILE=aa365-admin AWS_REGION=us-west-1 npm run test:signed-in
```

What it checks: (a) the shell and `GET /api/booking/staff/me` (role `admin`, not a preview session); (b) Costs & profit,
its three tabs (rows or the kit empty state, never the load error) and a trip case's cost in USD; (c) a trip plan's
Departures tab, the departure drawer (rules, allotments) and the B2B desk "Departures at risk" tile; (d) the agency
booking pages (B2B desk "Agency bookings" tab, departure manifest), skipped with the reason while not deployed. Every
test fails on an uncaught page error, a same-origin 5xx or any non-GET request. Screenshots at 1440 and 390, light and
dark, land in `e2e/results/signed-in/` (`BOOKING_E2E_SHOTS_DIR` to change), one JSON per shot in `report/`.

## The account

- WORKFORCE staff user **`e2e-booking@e2e-mail.lumiguides.it.com`**, Cognito group **`booking-admin` only**
  (root ADR 0006a A10a, AA-910). `booking-admin` is the least-privileged Booking group that reads every staff page,
  `/staff` and `/audit-log` included (`backend/app/auth/staff.py`).
- The WORKFORCE triggers (AA-CIS-Infra) issue a token for it only while its groups are exactly `booking-admin`; any
  other group set is refused. It has no `cis-*` group, so the CIS admin refuses it (403 `no_cis_group`).
- **No password.** Staff sign in with an email one-time code only; the code goes to the address itself, which is a
  mailbox of the SES inbound test domain (AA-CIS-Infra `e2e_mailbox.tf`): SES → S3 bucket `aa-cis-dev-e2e-mail-<acct>`,
  prefix `inbound/`, 1-day expiry. There is no secret to read.
- **Blast radius.** `booking-admin` is Booking's full write admin: `staff.admin`, `retention.admin` (DRY RUN and live
  delete / redaction of Booking rows), `b2b.admin`, `customer.admin` and every write. Anyone who can read a sign-in
  code gets that role on the production client: the mailbox reader role (AA-CIS-App and AA-Partner, environment
  `e2e`, branch `main`), the MFA admin role, and whoever controls mail for `e2e-mail.lumiguides.it.com`.
- **Read-only session, enforced (AA-910).** The backend refuses every request other than GET / HEAD / OPTIONS from
  this user with 403 `e2e_read_only`, on every client, before any DB access (`backend/app/auth/staff.py`,
  `E2E_TEST_STAFF_EMAIL`). Reads keep the admin permissions, so `/staff` and `/audit-log` still load. Write buttons
  still show in the UI; clicking one fails with that 403. The smoke must not test writes with this user.
- The first sign-in provisions its `booking.staff_users` row (`cog:<sub>`, admin, active). It appears on `/staff`.
- **It is assignable.** That row also appears in the StaffPicker (`staff.directory`), so real staff can assign trip
  cases to it. If it shows up in assignment, deactivate its Booking row on `/staff` between runs (the next sign-in
  is then 403 `staff_inactive` until it is re-activated).

## Flow to automate

1. Open `https://booking.lumiguides.it.com/login` (production client `booking-staff`, the ECS staff web; the
   only registered callback host since the Vercel retirement, AA-911).
2. Click **"Sign in with company email"** → managed login on `staff-auth.lumiguides.it.com`.
3. Snapshot the mailbox keys, type the address, ask for a code.
4. Poll `inbound/` for a NEW message addressed to `e2e-booking@…`, pull the 6–8 digit code, type it.
5. Back on the app: assert the shell renders and `GET /api/booking/staff/me` says `role: "admin"`,
   `preview_viewer` false; then visit the pages under test (read-only: the backend refuses this user's writes with
   403 `e2e_read_only`).

## Helpers (copied, not imported across repos)

AA-CIS-App solved steps 2–4; `signed-in/helpers/` holds copies (the header of each names the CIS commit):

- `tests/e2e/helpers/mailbox.ts` — S3 list/get through the AWS CLI (`execFile`, no npm dependency), MIME decode,
  recipient check, `waitForSignInCode({ since, baseline, recipient })` with a hard 90 s cap. Pass
  `recipient: 'e2e-booking@e2e-mail.lumiguides.it.com'` (the mailbox also holds the CIS and CRM test users' mail).
- `tests/e2e/helpers/cognitoSignIn.ts` — managed-login steps (`reachCodeStep`, `signInByEmailCode`). Make the
  address a parameter instead of the CIS constant.

The project `signed-in` has `testMatch: /signed-in\/.*\.spec\.ts$/`, `baseURL` from `BOOKING_E2E_BASE_URL` (default the
production host; set by the npm script, which also keeps the local server off), `workers: 1`, `retries: 1`, and depends
on the `storageState` setup project, so the code is requested once per run (the setup has `retries: 0`).

Rules (lessons log, Frontend): every wait has its own cap; never `networkidle`; fill-until-kept on inputs that
hydrate late. The sign-in code is a live credential: never log it, never screenshot the managed-login page after it
is typed, no trace on that step. Both projects run with `trace: 'off'` and `video: 'off'` (a trace records request
headers and cookies, which carry the session).

## Running it as an agent (local, no CI)

AWS credentials of the MFA admin profile can read the mailbox (the bucket policy allows only the reader role and
the admin role). Run with `AWS_PROFILE=aa365-admin AWS_REGION=us-west-1` and
`BOOKING_E2E_BASE_URL=https://booking.lumiguides.it.com`. Only one run at a time (two runs race for the same codes).

## Making it a GitHub Actions job (later, not in this change)

1. AA-CIS-Infra: add this repo's subject `repo:<org>@<org id>/AA-Booking@<repo id>:environment:e2e` to the trust
   of the mailbox reader role (same pattern as AA-Partner), Terraform Apply.
2. This repo: GitHub environment `e2e` (branch policy `main`), secret `E2E_MAIL_ROLE_ARN` = that role's ARN.
3. Workflow job: `environment: e2e`, `permissions: id-token: write, contents: read`, `concurrency` group shared
   by every run, `aws-actions/configure-aws-credentials` with `role-to-assume: ${{ secrets.E2E_MAIL_ROLE_ARN }}`,
   region `us-west-1`, then `npx playwright test --project=signed-in`. Trigger: after `Deploy Admin Dev` on `main`
   and `workflow_dispatch`. The role trusts `refs/heads/main` only, so PR runs cannot use it.
