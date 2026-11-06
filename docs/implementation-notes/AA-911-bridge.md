# AA-911 — retire the CIS -> Booking sign-in bridge, Booking side (lane l7-aa911-staff-ecs, S271)

Merge after the staff web cut-over (root runbook `docs/ops/runbooks/staff-web-ecs-cutover.md`) and BEFORE the
AA-CIS-App PR that removes `/auth/booking-token` (so Booking never offers a form whose upstream is gone). Fresh slice
on main; the parked branch `refactor/aa-911-retire-bridge` (S248) was far behind and mixed with the retired Preview
sign-in design.

## Decisions

- **Off first, delete later:** `identity_config.BRIDGE_ENABLED = False`. The production verifier holds the WORKFORCE
  issuer only, so a bridge token is 401 before any DB access (`tests/unit/test_aa911_bridge_off.py`).
- The ~25 endpoint test files sign in with bridge-shaped tokens (`tests/staff_tokens.py LocalCis.token()`). Moving
  them to WORKFORCE tokens changes staff row keys (`cog:<sub>`) and role mapping in each file: its own slice. Until
  then `LocalCis.verifier()` passes `include_bridge=True` explicitly (test issuer only); the bridge profile and
  provisioning code stay for it and are deleted with that slice.
- **Frontend:** the legacy CIS password path is removed: `POST /api/auth/login`, `LoginForm`, the bridge re-mint in
  `proxy.ts` and the BFF (`mintBookingToken`), `cisApiUrl()`. `bk_cis_session` is kept only as a name to clear: a
  browser that still holds it (or a non-Cognito `bk_access`) is sent to `/login?reason=expired` with the cookies
  cleared.

## Tradeoffs

- Keeping the bridge code for tests is a temporary inconsistency (tests exercise an issuer production refuses); the
  guard test pins the production behaviour so it cannot drift back.

## Should know

- Local checks (S271): backend unit suite 2156 passed, ruff check + format clean; integration tests of the staff
  identity and workflow modules on local Postgres: same result as main (4 pre-existing agency-billing failures on
  main too). Frontend: tsc, eslint of the touched files and the unit tests pass in the image build stage.
- Live after deploy: a bridge-shaped token (any `aud=booking` RS256 JWT) on `GET /booking/staff/me` = 401; the Booking
  login page shows only "Sign in with company email".
