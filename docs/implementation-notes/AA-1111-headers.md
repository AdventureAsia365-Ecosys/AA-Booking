# AA-1111 — security headers slice (S284, lane l5-aa1111-headers)

Source: the S283 ZAP baseline triage (root `docs/audits/2026-11-17-S283-access-review/zap-baseline.md`). Same slice
as the CIS admin and API (AA-CIS-App `docs/implementation-notes/AA-1111-headers.md`).

## Decisions

- **Nonce CSP on the staff admin.** `frontend/proxy.ts` makes a 128-bit nonce per request and sends the full policy
  (`frontend/lib/securityHeaders.ts`, same builder as the CIS admin) on the response and on the request (Next reads
  the nonce from the request CSP for its own scripts); `app/layout.tsx` puts it on the theme and sidebar restore
  scripts. `script-src 'self' 'nonce-…' 'strict-dynamic'`: no `'unsafe-inline'` for scripts.
- **style-src keeps `'unsafe-inline'`** (kit inline styles); **img-src** allows any https host; every other fetch
  directive is explicit (`connect-src` = self + the Sentry DSN origin; the browser only calls the same-origin BFF).
- **Matcher** now covers every path but `/api/*` and the build chunks. `/favicon.ico`, `/robots.txt` and
  `/assets/*` (outside the old matcher) pass without the session gate (`isStaticFile`). `/api/*` stays out (a running
  proxy clones the body and cuts it at 10 MiB); it and `/_next/*` get the policy without a nonce from `next.config.ts`.
- **HSTS stays two years with sub-domains, no preload** (the value every app already sends).
- **API**: `backend/app/observability/security_headers.py` (`nosniff` + HSTS), registered just inside the request id
  middleware; the unhandled 500 handler adds them itself. uvicorn runs with `--no-server-header`.

## Changed

- The root layout is `async` and reads `headers()`; every page renders per request (they already did, but the login
  shell).
- The public smoke compares the static `next.config.ts` headers as before and checks the CSP separately (nonce, no
  `'unsafe-inline'` for scripts, `default-src 'self'`); it also checks `/robots.txt`, an unknown path and the BFF.

## Tradeoffs

- No settings row for the CSP (the decision said not to invent one).
- No CORP / COEP on the API: an agency domain's pages may load API content cross-site.

## Should know

- Local check (next start, Chromium): every script on `/login` carries the nonce, the page hydrates, an injected inline
  handler is refused. The local public smoke's "Sign in with company email" step fails without the Cognito client
  secret (expected; the deployed host has it).
- The 403s ZAP saw from `awselb` on `booking.` are WAF block responses: AA-CIS-Infra adds the headers to the WAF
  custom responses and to the API Gateway gateway responses in the same wave.

## S294 (lane l16-sec-headers, ZAP re-run of 25/11)

- **Decisions:** no header change in the staff admin. Live `curl` on `/`, `/login`, `/robots.txt`, `/sitemap.xml` and a
  404 shows the full set already (nonce CSP, HSTS two years with sub-domains, Permissions-Policy, COOP, no
  `X-Powered-By`, no `Server`).
- **Root cause of the S294 ZAP alerts** on `booking.` (`/`, `/robots.txt`, `/sitemap.xml` "not set" and
  `Server: awselb/2.0`): ZAP's first requests carry no `User-Agent`; the WAF managed rule `NoUserAgent_HEADER` blocks
  them with the ALB's plain 403 (WAF sampled requests of the scan window). The managed rule group has no custom
  response yet: AA-CIS-Infra follow-up.
- **Changed:** the public smoke also checks `/` and `/sitemap.xml` (the scan's first paths).
