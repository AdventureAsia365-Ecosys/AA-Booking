import type { NextConfig } from "next";
import path from "path";

const nextConfig: NextConfig = {
  // The frontend lives in a sub-folder of the AA-Booking repo; pin the Turbopack root to it.
  turbopack: {
    root: path.resolve(__dirname),
  },
  poweredByHeader: false,
  async headers() {
    // AA-879: the same six security headers as the CIS admin, the CIS portal and TripPlanner (AA-876 /
    // AA-885), checked on every deployment by .github/scripts/check_security_headers.sh. The CSP is
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
    // AA-911: Preview builds (served on booking-preview.lumiguides.it.com when aliased) are never indexed.
    if (process.env.VERCEL_ENV === "preview") {
      headers.push({ key: "X-Robots-Tag", value: "noindex, nofollow" });
    }
    return [{ source: "/:path*", headers }];
  },
};

export default nextConfig;
