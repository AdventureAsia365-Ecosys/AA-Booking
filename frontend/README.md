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

Set in Vercel (or `.env.local` for local runs). All are optional; defaults are in `lib/config.ts`.

| Name | Where | Default | What |
|---|---|---|---|
| `BOOKING_API_URL` | server | Dev execute-api stage `https://9z3xnalp59.execute-api.us-west-1.amazonaws.com/dev` | Booking API base; `/booking/...` is appended |
| `CIS_API_URL` | server | `https://api-cis.lumiguides.it.com` | CIS API for `/auth/admin-login` and `/auth/booking-token` |
| `NEXT_PUBLIC_CIS_ADMIN_URL` | browser | `https://aa-cis.lumiguides.it.com` | "Open CIS admin" / "Managed in CIS" links |
| `NEXT_PUBLIC_GOOGLE_SIGNIN_URL` | browser | unset | Google sign-in start URL (AA-703). Unset = no "Continue with Google" button |

No secret is needed: the Booking JWT is minted by CIS per user and kept in an httpOnly cookie.

## Layout

```text
app/
  login/                 sign-in page (public)
  api/auth/login|logout  BFF: CIS login → Booking token → httpOnly cookies
  api/booking/[...path]  BFF proxy to <BOOKING_API_URL>/booking/<path> with the Bearer token
  (staff)/layout.tsx     GET /booking/staff/me → shell, or pending-activation / unavailable screens
  (staff)/**/page.tsx    every route of UI spec §3 (wave 1 real, wave 2/3 layout frame)
proxy.ts                 Next 16 middleware: no session → /login; refreshes the Booking token
lib/                     config, session cookies, rbac (page roles), typed API client + types
ui/                      copied CIS kit (byte copies, header names the CIS commit) + Booking shell
ui/booking/              Booking components (StatusBadge, Stepper, FilterBar, ConfirmDialog, FormDrawer, …)
```

## Rules

- Page code never uses a raw hex colour, a raw `<table>` or a hand-rolled dialog (eslint).
- Copied kit files in `ui/` change only their imports and storage keys; brand values change in CIS first.
- No mock data: a page whose endpoint does not exist yet says so in an empty state.
