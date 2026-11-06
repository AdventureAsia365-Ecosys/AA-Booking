# AA-911 — Booking admin on ECS (lane l7-aa911-staff-ecs, S271)

Root ADR 0019 d23 (+ S271 amendment): the Booking admin and the CIS admin leave Vercel and run on Dev as one Fargate
Spot service each (AA-CIS-Infra `modules/environment/staff_web.tf`): `aa-booking-dev-admin` (container
`booking-admin` :3001) and `aa-cis-dev-admin` (`cis-admin` :3000). Infra side:
AA-CIS-Infra `docs/implementation-notes/AA-911-staff-ecs.md`; CIS side: AA-CIS-App, same file name.

## Decisions

- `frontend/Dockerfile` follows the AA-Partner web image (Next standalone, node 22 alpine, non-root, npm removed from
  the runtime, `KEEP_ALIVE_TIMEOUT` above the ALB idle timeout, `HOSTNAME=0.0.0.0` forced in CMD), port 3001 (kept
  from the first draft, where both apps shared one task; the Infra security group admits 3000 and 3001). No build argument: nothing public needs baking in.
- `output: "standalone"` only when `NEXT_OUTPUT=standalone` (set by the Dockerfile): Vercel builds unchanged until the
  Vercel project is retired.
- `/api/healthz` (no auth, no upstream, returns the commit), outside `proxy.ts` (its matcher skips `/api/`).
- `deploy-admin-dev.yml` is separate from `deploy-dev.yml` (the API): no job guard (the admin runs no `booking.job`),
  own concurrency group, copies the latest revision of its own family `aa-booking-dev-admin` and swaps the image
  (S271 fix pass: per-app families keep the deploy roles apart and remove the "other container still on bootstrap"
  stop); when the service runs no task (desired 0, before the scale-up) the verify job is skipped and the result says
  so. Deploy role = the existing Booking deploy role (its extra grant
  is in AA-CIS-Infra `staff_web.tf`).
- Same public host as today (`booking.lumiguides.it.com`): `lib/cognito.ts` needs no change (outside Vercel the
  production client and its hosts are the allowlist).

## Changed

- **Found while testing the image:** behind the ALB, Next standalone builds `request.url` / `nextUrl` from its bind
  address (`https://0.0.0.0:3001`). Every BFF write and sign-out would be refused by `isSameOrigin`, and the
  sign-in / callback / refresh redirects would point at `0.0.0.0`. Fixed: `requestOrigin` (public origin from `Host`
  when the request came over HTTPS) and relative Location redirects (`lib/redirect.ts`).
- `npm run test:unit` also runs `lib/*.test.mjs` (the new `lib/sameOrigin.test.mjs`).

## Tradeoffs

- `Host` is trusted only for the comparison with the browser-set `Origin`; a cross-site page controls neither.
- One task for both staff apps: a crash or deploy of either restarts both (accepted for Dev, ADR 0019 d23).

## Should know

- Local check (S271): `docker build` passed (tsc + eslint of the touched files + unit tests in the build stage); the
  container answered `/api/healthz` with the build commit, `/login` 200, healthy with a foreign `HOSTNAME`; sign-in,
  refresh and page redirects are relative; a same-origin POST passed, a sibling-host POST got 403.
- GitHub `dev` environment: `vars.STAFF_WEB_ENABLED=true` (after the Infra apply) and `vars.STAFF_WEB_ALB_DNS`.
