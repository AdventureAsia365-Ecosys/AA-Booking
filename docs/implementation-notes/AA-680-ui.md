# AA-680 UI — Booking staff "Sign in with company email" (Cognito booking-staff)

Session S243 (Claude Code builder). Part of AA-680 Booking Phase 2 (root ADR 0006 / 0006a). Backend side and the
PR-4 bridge-retirement checklist: `AA-680.md` (branch `feat/aa680-cognito-staff`). Branch:
`feat/aa680-staff-company-email-signin` (frontend only).

## Decisions

- **Port of CIS `frontend/lib/cognito.ts` (#811, AA-816 C4), booking-staff client only**, into `lib/cognito.ts`:
  same allowlist rule (redirect URI from a fixed list, the Host header only selects), PKCE S256, state + nonce in one
  httpOnly transaction cookie (`bk_oauth_tx`, path `/api/auth/callback`, 5 min), ID-token nonce / aud / iss checks,
  access-token `token_use` / `client_id` (and `iss`) checks, HTTP Basic client auth, single-flight refresh with a
  20 s reuse window, best-effort revoke, Cognito `/logout`. Public values are code constants with optional env
  overrides (client id `5ifl9dgkr8a5outnmsjc8v1kt3`, domain `https://staff-auth.lumiguides.it.com`, the Dev WORKFORCE
  issuer); only the client **secret** must be set in Vercel. No `server-only` package (not a dependency here); only
  route handlers and the server login page import `lib/cognito.ts`.
- **Allowed origins** = the booking-staff client's registered callbacks (AA-CIS-Infra identity_variables.tf):
  `https://booking.lumiguides.it.com`, `https://aa-booking-admin.vercel.app`, plus `http://localhost:3001` under
  `next dev` only. Vercel previews get no company sign-in (not registered): `/api/auth/signin/cognito` sends them back
  to `/login?reason=cognito_unavailable`.
- **The callback asks the Booking API who this is** (`GET /booking/staff/me` with the new access token) instead of
  CIS `verify-admin`: Booking decides the role from the booking-* group and creates / finds the staff row (bk-016).
  403 `no_booking_role` (and any other 403 except `staff_inactive`, and 401) → no session, the refresh token is
  revoked, `/login?reason=…`. `staff_inactive` and "API not answering" → session cookies, the (staff) layout shows the
  matching screen (same as the legacy flow's "pending").
- **Post-login path in the transaction cookie** (`x`, passed through `safeNext` both ways), so `?next=` survives the
  round trip to Cognito.
- **Refresh cookie `bk_staff_rt` path `/api`, not `/api/auth`** (plan / CIS). The Booking BFF proxy
  (`/api/booking/*`) must refresh by itself, like it re-mints the legacy token today: the client-side API calls
  (react-query) would otherwise get a 401 after 15 minutes and bounce to `/login`. CIS solves it with a keep-alive
  POST from the shell; one path covering both routes is simpler. Page requests still never receive it.
- **`bk_access` holds the Cognito access token and lives for the 12 h session** (CIS pattern): `proxy.ts` must still
  see an expired token to know it should go through `/api/auth/refresh?next=…` (the only place with the refresh
  cookie besides the BFF). The session kind is read from the token itself (`token_use = access` + `client_id`, no
  signature check; the API verifies everything).
- **One session kind at a time**: the company callback clears `bk_cis_session`; the legacy login clears
  `bk_staff_rt`. `clearSessionCookies()` clears all three everywhere a session ends.
- **The legacy CIS password form stays reachable on production, collapsed** under the company button ("Use a CIS
  username and password instead (legacy)"), and is the only form where company sign-in is unavailable (previews,
  secret unset). The plan said "fallback only where Cognito is unavailable"; kept on production too because the
  Booking UI smoke / E2E still signs in through the bridge with the E2E account until AA-680 PR-4 (no Cognito E2E
  mailbox yet, AA-816 D2).
- **Google placeholder removed** (`NEXT_PUBLIC_GOOGLE_SIGNIN_URL`, `ui/GoogleMark.tsx`): staff never sign in with
  Google (ADR 0006a A9).
- **403 screens**: `no_booking_role` → "Your account has no Booking role" + link to CIS → Platform → Staff
  (`/admin/platform/staff`); `staff_inactive` copy no longer says "CIS account" (also used for suspended profiles).
- **`/staff` and `/staff/{id}`**: for a profile with `managed_in_directory` (API field, AA-680 backend), Change role is
  disabled / replaced by the CIS link; Activate / Deactivate stays (Booking-side switch). The detail shows the sign-in
  kind and labels the key "Staff key" (it is `cog:<sub>` for profiles created by a company sign-in).

## Changed (vs. the plan)

- Refresh cookie path `/api` instead of `/api/auth` (BFF refresh, see Decisions).
- Legacy form kept (collapsed) on production hosts, not only on previews.
- No `/api/auth/refresh` POST keep-alive (CIS has one); the BFF refresh covers client-side calls.
- `CONTEXT.md` "Staff admin frontend" section edited here (this builder owns Booking CONTEXT.md; the backend PR edits
  other sections only, so the two PRs merge without conflict).

## Tradeoffs

- No unit test runner in `frontend/` (not added in this PR, per the plan): checked with eslint, tsc, `next build` and a
  local `next start` smoke (below). The real flow can only be proven on the production host with the secret set.
- An expired-but-present `bk_access` cookie (up to 12 h) is harmless (the API rejects it) and is what lets the proxy
  route a page to the refresh; the refresh token itself never reaches page requests.
- A refresh while Cognito is unreachable sends a page to `/login?reason=unavailable` without clearing the session
  (going back to the page would loop through the refresh).

## Should know

- ⚠️ **Underscore env var, set by hand (Nghiệp, Vercel UI)**: `COGNITO_BOOKING_STAFF_CLIENT_SECRET` on project
  `aa-booking-admin` (Production; Preview is pointless because previews are not registered callbacks). Value: the
  booking-staff client secret (Cognito console → pool `aa-workforce-dev` → App clients →
  `aa-workforce-dev-booking-staff` → Show client secret). Without it the button is hidden and the legacy form shows
  (safe default). The optional overrides `COGNITO_BOOKING_STAFF_CLIENT_ID`, `COGNITO_WORKFORCE_DOMAIN`,
  `COGNITO_WORKFORCE_ISSUER` stay unset.
- **Merge order**: after the backend PR (`feat/aa680-cognito-staff`) is deployed (the callback needs a Booking API
  that accepts Cognito tokens; otherwise `/booking/staff/me` answers 401 and the sign-in ends at
  `/login?reason=signin_failed`, harmless). Then set the secret, then merge (vercel-deploy.yml deploys production).
- Local Cognito sign-in: the client registers `http://localhost:3001/...`, but `npm run dev` uses port 3100; run
  `npx next dev --port 3001` to try it locally (an Infra callback change is deferred).
- Local smoke done (`next start`, dummy secret, Host header): `/api/auth/signin/cognito?next=/cases` on
  `booking.lumiguides.it.com` → 303 to `staff-auth.lumiguides.it.com/oauth2/authorize` with client_id
  `5ifl9dgkr8a5outnmsjc8v1kt3`, redirect_uri `https://booking.lumiguides.it.com/api/auth/callback/cognito`,
  `code_challenge_method=S256`, tx cookie `Path=/api/auth/callback; Max-Age=300; Secure; HttpOnly`; `/login` shows the
  company button + the collapsed legacy form; a preview host shows only the legacy form and its signin → 303
  `/login?reason=cognito_unavailable`; an expired Cognito `bk_access` on `/cases?x=1` → 307
  `/api/auth/refresh?next=%2Fcases%3Fx%3D1`; refresh without `bk_staff_rt` → `/login?reason=expired`; BFF without a
  session → 401.
- Live checks after deploy: `curl -sI https://booking.lumiguides.it.com/api/auth/signin/cognito` → 303 to
  staff-auth `/oauth2/authorize` (as above); same on `aa-booking-admin.vercel.app`; screenshot of `/login` with the
  button; a preview URL shows only the legacy form; Nghiệp's sign-in lands on the dashboard; logout ends at staff-auth
  `/logout` → `/login`.

## PR-4 (bridge retirement) — frontend part

- [ ] Remove `LoginForm.tsx` and the `<details>` legacy block, `app/api/auth/login/route.ts`, `mintBookingToken`,
      `CIS_SESSION_COOKIE` / `CIS_SESSION_MAX_AGE_S`, the bridge branches of `proxy.ts` and the BFF proxy, `cisApiUrl`
      / `CIS_API_URL` (if nothing else uses it).
- [ ] UI smoke / E2E on a Cognito path (AA-816 D2 mailbox) or Nghiệp's manual sign-in.
- [ ] Optionally register the `next dev` port 3100 callback (Infra) instead of 3001.
