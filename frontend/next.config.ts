import type { NextConfig } from "next";
import path from "path";
import { STATIC_SECURITY_HEADERS, contentSecurityPolicy, sentryOrigin } from "./lib/securityHeaders";

// AA-1111 (S284, ZAP baseline): the paths proxy.ts skips (the BFF under /api, the build chunks under /_next) get the
// full policy without a nonce (no inline script is served there); every other path gets it with a nonce from proxy.ts.
const STATIC_CSP = contentSecurityPolicy({ connectSrc: [sentryOrigin(process.env.NEXT_PUBLIC_SENTRY_DSN)] });

const nextConfig: NextConfig = {
  // The frontend lives in a sub-folder of the AA-Booking repo; pin the Turbopack root to it.
  turbopack: {
    root: path.resolve(__dirname),
  },
  // AA-911 (root ADR 0019 d23): the ECS image (frontend/Dockerfile) builds the standalone server; the CI builds
  // (Frontend Build, Visual's `next start`) keep the default output.
  ...(process.env.NEXT_OUTPUT === "standalone" ? { output: "standalone" as const } : {}),
  poweredByHeader: false,
  async headers() {
    // AA-879: the same security headers as the CIS admin, the CRM web and TripPlanner (AA-876 / AA-885), checked after
    // every deploy by the public smoke (e2e/smoke-public/login.spec.ts). AA-1111 (S284): the list lives in
    // lib/securityHeaders.ts (+ Cross-Origin-Opener-Policy); the Content-Security-Policy is the full policy, sent with
    // a per-request nonce by proxy.ts. Staff sign-in leaves through a redirect (Cognito managed login), not a form
    // post, so form-action 'self' does not touch it.
    const csp = [{ key: "Content-Security-Policy", value: STATIC_CSP }];
    return [
      { source: "/:path*", headers: STATIC_SECURITY_HEADERS },
      { source: "/api/:path*", headers: csp },
      { source: "/_next/:path*", headers: csp },
    ];
  },
};

export default nextConfig;
