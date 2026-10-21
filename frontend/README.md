# AA-Booking staff admin (frontend)

Next.js 16 (App Router) + React 19 + TypeScript + Tailwind v4, react-query and TanStack Table.
Design: the adventure.asia brand tokens of the CIS kit, laid out like the Booking Figma (UI spec S236).
Decisions and trade-offs: `../docs/implementation-notes/AA-683.md`.

## Run

```bash
npm ci
npm run dev        # http://localhost:3100
npm run lint       # eslint, no warnings allowed
npm run typecheck  # tsc --noEmit
npm run build      # next build
```

Sign in with a CIS staff account. Locally the app talks to the Dev Booking API and the live CIS API unless
the server environment points elsewhere (see below).

## Environment

Set in Vercel (or `.env.local` for local runs). All are optional; defaults are in `lib/config.ts` and
`lib/cognito.ts`.

| Name | Where | Default | What |
|---|---|---|---|
| `BOOKING_API_URL` | server | Dev custom domain `https://api-booking.lumiguides.it.com` | Booking API base; `/booking/...` is appended |
| `NEXT_PUBLIC_CIS_ADMIN_URL` | browser | `https://aa-cis.lumiguides.it.com` | "Open CIS admin" / "Managed in CIS" links |
| `COGNITO_BOOKING_STAFF_CLIENT_SECRET` | server, **secret**, Production target only | unset | Secret of the Cognito `booking-staff` app client (AA-680). Never on the Preview target (AA-911 D-1). Unset = no staff sign-in on that build |
| `COGNITO_BOOKING_STAFF_CLIENT_ID` | server | `5ifl9dgkr8a5outnmsjc8v1kt3` | booking-staff client id (Dev); override only for another pool |
| `COGNITO_BOOKING_STAFF_PREVIEW_CLIENT_SECRET` | server, **secret**, Preview target only | unset | Secret of the Preview-only `booking-staff-preview` client (AA-911). A preview build reads only this one, never the production secret |
| `COGNITO_BOOKING_STAFF_PREVIEW_CLIENT_ID` | server | code constant (empty until the Infra apply) | booking-staff-preview client id; empty = no company sign-in on preview builds |
| `COGNITO_WORKFORCE_DOMAIN` | server | `https://staff-auth.lumiguides.it.com` | managed-login domain |
| `COGNITO_WORKFORCE_ISSUER` | server | the Dev WORKFORCE pool issuer | checked against the ID / access token `iss` |

Company e-mail sign-in is the only staff sign-in (the CIS password bridge is retired, AA-911). It is offered only
on the registered callback hosts when the client secret is set: a production build on `booking.lumiguides.it.com`
and `aa-booking-admin.vercel.app`, a preview build on the fixed Preview host `booking-preview.lumiguides.it.com`
(aliased by the Vercel Deploy workflow on opt-in), plus `localhost:3001` under `next dev`. Tokens stay in httpOnly
cookies (`bk_access`, `bk_staff_rt`; a leftover `bk_cis_session` from the retired bridge is only cleared).

## Layout

```text
app/
  login/                 sign-in page (public)
  api/auth/signin|callback/cognito, refresh   company e-mail sign-in (Cognito booking-staff, PKCE) + token refresh
  api/auth/login|logout  BFF: legacy CIS login → Booking token → httpOnly cookies; logout (revokes Cognito)
  api/booking/[...path]  BFF proxy to <BOOKING_API_URL>/booking/<path> with the Bearer token
  (staff)/layout.tsx     GET /booking/staff/me → shell, or pending-activation / unavailable screens
  (staff)/**/page.tsx    every route of UI spec §3 (wave 1 real, wave 2/3 layout frame)
proxy.ts                 Next 16 middleware: no session → /login; Cognito token near expiry → /api/auth/refresh;
                         re-mints the legacy Booking token
lib/                     config, session cookies, rbac (page roles), typed API client + types
ui/                      copied CIS kit (byte copies, header names the CIS commit) + Booking shell
ui/booking/              Booking components (StatusBadge, Stepper, FilterBar, ConfirmDialog, FormDrawer, …)
```

## Rules

- Page code never uses a raw hex colour, a raw `<table>` or a hand-rolled dialog (eslint).
- Copied kit files in `ui/` change only their imports and storage keys; brand values change in CIS first.
- No mock data: a page whose endpoint does not exist yet says so in an empty state.
