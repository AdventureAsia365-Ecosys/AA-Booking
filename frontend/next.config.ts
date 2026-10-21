import type { NextConfig } from "next";
import path from "path";

const nextConfig: NextConfig = {
  // The frontend lives in a sub-folder of the AA-Booking repo; pin the Turbopack root to it.
  turbopack: {
    root: path.resolve(__dirname),
  },
  poweredByHeader: false,
  async headers() {
    const headers = [
      { key: "X-Frame-Options", value: "DENY" },
      { key: "X-Content-Type-Options", value: "nosniff" },
      { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
    ];
    // AA-911: Preview builds (served on booking-preview.lumiguides.it.com when aliased) are never indexed.
    if (process.env.VERCEL_ENV === "preview") {
      headers.push({ key: "X-Robots-Tag", value: "noindex, nofollow" });
    }
    return [{ source: "/:path*", headers }];
  },
};

export default nextConfig;
