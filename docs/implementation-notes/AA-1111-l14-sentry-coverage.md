# AA-1111 — lane l14 Sentry coverage: Booking staff admin (frontend/)

S282 observability hygiene. The Booking API and its job worker already report to Sentry (project aa-booking-api,
`SENTRY_DSN` from Infra). The staff admin (`frontend/`, ECS service aa-booking-dev-admin) had no Sentry SDK and
reported nothing. This PR adds it the way the AA-Partner web does. The orchestrator creates the Sentry project
aa-booking-admin and sets the `dev` variable.

## Decisions

- `@sentry/nextjs` 11.11.0, pinned exactly: the same version as the AA-Partner web (`dataCollection` API). The
  lockfile adds the SDK tree. One existing entry moves: `@types/estree` 1.0.9 to 1.0.10, a type-only transitive
  dependency.
- `instrumentation.ts` initialises the server SDK in the nodejs runtime and exports
  `onRequestError = Sentry.captureRequestError`. `instrumentation-client.ts` initialises the browser SDK. No
  `withSentryConfig` and no legacy `sentry.*.config.ts`: the same as the AA-Partner web. Next 16 loads both hooks
  natively, and the local probe below shows that this is enough.
- One options builder, `lib/sentryConfig.ts`. It is a copy of the CIS admin's `frontend/lib/sentryConfig.ts`.
  An empty DSN returns `null`, so `Sentry.init` is skipped and nothing is sent. `dataCollection` is off for user,
  cookies, headers, query strings, bodies and frame variables. `beforeSend: scrubEvent` drops the user, cookies,
  credential headers and the body, plus the query string of the request URL, of `contexts.nextjs.request_path` and of
  breadcrumb URLs. `tracesSampleRate: 0` (errors only).
- DSN: one public value, the Docker build arg `NEXT_PUBLIC_SENTRY_DSN`. Deploy Admin Dev fills it from the `dev`
  variable `ADMIN_PUBLIC_SENTRY_DSN`, and it is empty by default. The server reads `SENTRY_DSN` first (a runtime
  mapping, if Infra ever adds one), else the inlined build value. No Infra change is needed.
- Environment: `ENVIRONMENT` on the server if the task sets one (today it does not), else the build arg
  `NEXT_PUBLIC_ENV` (default `dev`). Release: `GIT_SHA` on the server, and the build arg `NEXT_PUBLIC_GIT_SHA` in
  the browser.

## Changed

- The task brief suggested a runtime secret `booking/<env>/admin-sentry-dsn` as one option for server errors. It is
  not needed: the build-time public DSN is inlined into the server bundle too.

## Tradeoffs

- The build-time DSN means a new value needs a new image. The ECR repository is IMMUTABLE, and a re-run of an
  already-built commit reuses its image. So the variable must be set before the commit that should carry it is
  built.
- The DSN is printed in the public Actions log (step env). A DSN is public by design: it is already in every
  browser bundle.

## Should know

- Local end-to-end proof: a standalone build with a DSN that points at a local fake ingest received one server event
  (a route that throws, through `onRequestError`) and one browser event (an uncaught error on `/login`). The SDK
  was `sentry.javascript.nextjs/11.11.0`, with `environment=dev` and the release tag. None of the query value,
  cookie or bearer token that the requests sent reached the ingest. The probe route was temporary and is not in the
  diff.
- Tests: `lib/sentryConfig.test.mjs` (options and scrub) and `lib/sentryWiring.test.mjs` (hooks, no legacy configs,
  build arg, workflow variable). Both run in `npm run test:unit` (CI Frontend Build).
