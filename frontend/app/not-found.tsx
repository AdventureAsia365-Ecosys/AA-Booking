// app/not-found.tsx — AA-683: unknown URL (outside the spec §3 route map).
import Link from "next/link";
import { StandaloneFrame } from "@/ui/local/StandaloneFrame";

export default function NotFound() {
  return (
    <StandaloneFrame className="bg-bg px-4">
      <div className="text-center">
        <p className="font-display text-5xl font-semibold text-accent">404</p>
        <h1 className="mt-2 text-lg font-semibold text-ink">This page does not exist</h1>
        <p className="mt-1 text-sm text-muted">Check the address, or go back to the dashboard.</p>
        <Link href="/" className="mt-5 inline-flex rounded-full bg-accent px-5 py-2 text-[13px] font-semibold uppercase tracking-[0.06em] text-on-accent hover:bg-accent-deep">
          Dashboard
        </Link>
      </div>
    </StandaloneFrame>
  );
}
