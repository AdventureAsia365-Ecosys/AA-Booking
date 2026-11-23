# AA-1176 — Dev-only E2E owner (root ADR 0006a A10h), AA-Booking part

## Decisions
- **A plain administrator, not an E2E writer.** `e2e-owner@e2e-mail.lumiguides.it.com` is NOT in `E2E_WRITERS` (no
  allow-list, no synthetic-record check, no route-scoped grant). The tech lead asked for "an admin like me"; the live
  steps (view as sales, a sale exception on a synthetic plan) need admin writes that no allow-list would cover.
- **Guards before any DB access** (`e2e_owner.check_session`, called in `current_staff` right after
  `_refuse_read_only_writes`): any principal whose e-mail trims and lower-cases to the address is held to
  `ENVIRONMENT == "dev"` exactly (403 `e2e_not_allowed_here`, GET included), the exact ASCII address and exactly
  `{cis-admin, booking-admin}` (403 `e2e_scope`). A non-ASCII look-alike can only reach this check if the trigger let
  it through, and is refused.
- **MFA exemption.** `admin_mfa.check_session(exempt=... or e2e_owner)`: the e-mail code is readable only by the MFA
  admin IAM role, which stands in for the authenticator code.
- **Owner marker from CIS, unchanged path.** `custom_roles.is_owner` reads `shared.v_staff_booking_grant` keyed by the
  Cognito sub, which lists only rows CIS has linked: sign in to CIS first.
- **Never self-approves.** `self_approval.eligible_session` returns False for the address (belt and braces: CIS never
  lets it be the business owner).
- **Recognisable rows.** The first-sign-in staff row is named `E2E owner (agent, Dev only)`, the `staff.provisioned`
  audit row carries `e2e_owner: true` and says "first sign-in of the Dev-only E2E owner". Every later write's audit row
  names it through its `cog:<sub>` staff row.

## Changed
- `backend/app/auth/e2e_owner.py` (new; `e2e_staff.py` is at the code-health file-size limit): constants,
  `is_e2e_owner`, `is_e2e_owner_like`, `check_session`.
- `backend/app/auth/staff.py`: the guard call, the MFA exemption, the provisioning name and audit.
- `backend/app/auth/self_approval.py`: refusal.
- `backend/tests/unit/test_aa1176_e2e_owner.py`; `CONTEXT.md`.
- No migration (bk-066 belongs to AA-1175).

## Tradeoffs
- No "synthetic only" rule in code for this identity: it would make it unlike an administrator and block the live
  checks. The rule is operational (root ADR 0006a A10h) and every row it writes names it.
- Maker-checker: the account can be the checker of a real person's price / policy version or payment. The ADR keeps it
  as an operating rule (maker yes, checker no); a code guard is a follow-up if the tech lead wants one.

## Should know
- Live order: CIS migration 473 and the CIS / Booking / Partner code live first, then the Infra trigger entry, the
  Cognito user, the CIS run-script (owner marker + Booking admin grant), then CIS sign-in, then Booking.
- Removal with the A10 family (AA-1054): drop the constants, `check_owner_session`, its call, the MFA exemption term and
  the self-approval line. At once: a CIS owner disables the account on CIS Platform › Staff.
