import type { NextConfig } from "next";
import path from "path";

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
    // AA-879: the same six security headers as the CIS admin, the CIS portal and TripPlanner (AA-876 /
    // AA-885), checked after every deploy by the public smoke (e2e/smoke-public/login.spec.ts). The CSP is
    // deliberately minimal (no script-src / style-src: Next inline scripts and inline styles); it only stops
    // framing, <base> hijacking, foreign form posts and plugins. Staff sign-in leaves through a redirect
    // (Cognito managed login), not a form post, so form-action 'self' does not touch it.
    const headers = [
      { key: "Strict-Transport-Security", value: "max-age=63072000; includeSubDomains" },
      { key: "X-Frame-Options", value: "DENY" },
      { key: "X-Content-Type-Options", value: "nosniff" },
      { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
      {
        key: "Content-Security-Policy",
        value: "frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'",
      },
      { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
    ];
    return [{ source: "/:path*", headers }];
  },
};

export default nextConfig;
