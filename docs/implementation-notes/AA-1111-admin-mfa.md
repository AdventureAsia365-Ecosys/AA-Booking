# AA-1111 slice S2 — Booking administrator MFA check (lane l17, S285)

Tech lead decisions: admin MFA = TOTP (S283); drop the WAF geo rule of the staff hosts once admin MFA is enforced
(S285). CIS and the CRM ship the same check behind their own switch.

## Decisions
- **Who is checked**: WORKFORCE sessions whose Booking role is `admin` (group `booking-admin`). Not checked: other
  roles, the Preview client (read-only, retired), the Dev-only E2E identities (AA-910 read-only admin; AA-1087 writers),
  the CIS bridge (retired path, no Cognito claims).
- **Claim**: `amr` holds `otp` (TOTP). Read from the verified token string with PyJWT (no second signature check):
  adding `amr` to `Principal` would change the byte-copied verifier (vendored-copy CI against CIS main).
- **Switch**: pricing policy parameter `staff_admin_mfa_required` (bk-061, boolean, section "security" = "Staff
  sign-in", approver group admin, seeded false). Booking has no other admin-editable settings store (S279 moved every
  policy value to Pricing > Policy), so the switch lives there with maker-checker and history.
- **Missing value reads off** (logged `staff_admin_mfa_switch_unavailable`), unlike the pricing readers (503): a 503
  here would lock out the administrators who approve the parameter. The readiness raise to bk-061 keeps the code from
  serving before the seed exists.
- **Cache**: the switch is read at most every 30 s per process (technical constant), and only when an admin session
  lacks the code.
- **Placement**: `current_staff`, before the staff row sync, so a refused session writes nothing.

## Changed
- `tests/unit/fakes.py` seeds `staff_admin_mfa_required = false` like the other bk- seeds.
- The policy page gains section "security" (EN "Staff sign-in", VI "Đăng nhập nhân viên").

## Tradeoffs
- Turning the switch on needs two administrators (maker-checker on every pricing policy change); the business owner
  holds Booking admin and can approve.

## Should know
- A token with no `amr` counts as "no MFA"; the Infra runbook's `staff-totp-check` proves the claim first.
