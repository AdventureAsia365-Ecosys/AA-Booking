// app/login/page.tsx
// AA-683 — staff sign-in (UI spec §4.1; Nghiệp S236 decision 2). Staff use their CIS credentials; the
// BFF turns them into a Booking session (app/api/auth/login). Visual: the adventure.asia look on the
// kit tokens: a cream-to-gold wash (the Figma "Group 1410084962" background, rebuilt in CSS), the
// Satisfy accent line (>= 32 px, design system §2.2), Fahkwang title, gold pill button. Pinned light,
// like the site and the CIS login (data-theme="light" re-scopes the --aa-* variables).

import type { Metadata } from "next";
import { LoginForm } from "./LoginForm";
import { GOOGLE_SIGNIN_URL, CIS_ADMIN_URL } from "@/lib/config";
import { safeNext } from "@/lib/safeNext";

export const metadata: Metadata = { title: "Sign in" };

export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const sp = await searchParams;
  const reason = typeof sp.reason === "string" ? sp.reason : "";
  return (
    <main data-theme="light" className="bk-login-bg relative grid min-h-screen place-items-center overflow-hidden px-4 py-10">
      <div className="w-full max-w-[440px]">
        <div className="mb-6 text-center">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src="/assets/adventure-asia-logo.png" alt="Adventure Asia" width={72} height={46} className="mx-auto h-auto w-[72px]" />
          <p className="mt-3 font-script text-[40px] leading-none text-accent">Welcome back</p>
        </div>
        <div className="rounded-lg border border-line bg-card px-7 py-8 shadow-[0_20px_60px_rgba(31,41,51,0.10)] sm:px-9">
          <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-accent-deep">Booking · staff</p>
          <h1 className="mt-1 font-display text-[26px] font-semibold leading-tight text-ink">Sign in to Booking</h1>
          <p className="mt-1.5 text-[13px] text-muted">Use your CIS staff account. Trip cases, proposals and customers in one place.</p>
          <LoginForm next={safeNext(sp.next)} expired={reason === "expired"} googleUrl={GOOGLE_SIGNIN_URL} />
        </div>
        <p className="mt-5 text-center text-xs text-muted">
          Passwords and new accounts are managed in{" "}
          <a href={CIS_ADMIN_URL} target="_blank" rel="noopener noreferrer" className="font-medium text-accent-deep hover:underline">
            CIS admin
          </a>
          .
        </p>
      </div>
    </main>
  );
}
