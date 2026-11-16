// instrumentation.ts
// AA-1111 (S282 Sentry coverage) — Sentry on the server of the Booking staff admin (project aa-booking-admin), the
// AA-Partner web pattern. register() runs once per server start (Next 16 loads it in a Turbopack build; the legacy
// sentry.server.config.ts is not loaded any more). The DSN: SENTRY_DSN when the task maps one at run time, else the
// public build-time NEXT_PUBLIC_SENTRY_DSN (Docker build arg from the `dev` variable ADMIN_PUBLIC_SENTRY_DSN, inlined by
// next build). None = no init, nothing is sent. environment = ENVIRONMENT, else the build arg NEXT_PUBLIC_ENV (dev);
// release = the image's git SHA;
// no PII, no tracing (lib/sentryConfig.ts).
import * as Sentry from "@sentry/nextjs";
import { sentryInitOptions } from "./lib/sentryConfig";

export async function register() {
  if (process.env.NEXT_RUNTIME !== "nodejs") return;
  const options = sentryInitOptions({
    dsn: process.env.SENTRY_DSN || process.env.NEXT_PUBLIC_SENTRY_DSN,
    environment: process.env.ENVIRONMENT || process.env.NEXT_PUBLIC_ENV,
    release: process.env.GIT_SHA,
  });
  if (options) Sentry.init(options);
}

// Errors of server components, route handlers and the proxy (a no-op while Sentry is not initialised).
export const onRequestError = Sentry.captureRequestError;
