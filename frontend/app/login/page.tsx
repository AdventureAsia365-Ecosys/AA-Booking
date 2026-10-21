// app/login/page.tsx
// AA-683 / AA-680 — staff sign-in (UI spec §4.1). Visual: the adventure.asia look on the kit tokens: a cream-to-gold
// wash (the Figma "Group 1410084962" background, rebuilt in CSS), the Satisfy accent line (>= 32 px, design system
// §2.2), Fahkwang title, gold pill button. Pinned light, like the site and the CIS login (data-theme="light"
// re-scopes the --aa-* variables).
//
// AA-680 (root ADR 0006 / 0006a): the only way in is "Sign in with company email" — the Cognito WORKFORCE managed
// login (booking-staff client, staff-auth.lumiguides.it.com, an e-mail code). It is offered only on the registered
// hosts with the client secret set (lib/cognito.ts). AA-911 (ADR 0006a A11): the CIS password bridge is retired;
// a per-deployment Vercel preview URL points staff at the fixed Preview host booking-preview.lumiguides.it.com.
// Booking roles are managed in CIS → Platform → Staff.

import type { Metadata } from "next";
import { headers } from "next/headers";
import { CIS_STAFF_URL } from "@/lib/config";
import { PREVIEW_ORIGIN, allowedOriginForHost, cognitoSignInAvailable, workforceSignInHost } from "@/lib/cognito";
import { safeNext } from "@/lib/safeNext";

export const metadata: Metadata = { title: "Sign in" };
export const dynamic = "force-dynamic";

// /login?reason=… set by proxy.ts, the BFF and the Cognito routes.
const REASONS: Record<string, { tone: "info" | "error"; text: string }> = {
  expired: { tone: "info", text: "Your session has ended. Sign in again to continue." },
  no_booking_role: {
    tone: "error",
    text: "Your company account has no Booking role yet. Ask an owner to give you one in CIS → Platform → Staff, then sign in again.",
  },
  forbidden: { tone: "error", text: "Booking refused this account. Ask a Booking admin to check your staff profile." },
  signin_failed: { tone: "error", text: "The company sign-in did not complete. Try again." },
  denied: { tone: "error", text: "The company sign-in was cancelled." },
  unavailable: { tone: "error", text: "The company sign-in service is not answering. Try again in a moment." },
  cognito_unavailable: {
    tone: "error",
    text: "Company e-mail sign-in is not available on this address.",
  },
};

export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const sp = await searchParams;
  const reason = REASONS[typeof sp.reason === "string" ? sp.reason : ""] ?? null;
  const next = safeNext(sp.next);
  const host = (await headers()).get("host");
  const company = cognitoSignInAvailable(host);
  // A registered host without the client secret (a preview built before the Preview env var was set) vs. a host that
  // can never sign in (a per-deployment *.vercel.app preview URL).
  const registeredHost = allowedOriginForHost(host) !== null;
  const signInHref = `/api/auth/signin/cognito${next !== "/" ? `?next=${encodeURIComponent(next)}` : ""}`;
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
          <p className="mt-1.5 text-[13px] text-muted">
            Use your company e-mail. Trip cases, proposals and customers in one place.
          </p>
          {reason && (
            <p
              role={reason.tone === "error" ? "alert" : "status"}
              className={`mt-5 rounded-md border px-3 py-2 text-[13px] ${
                reason.tone === "error"
                  ? "border-danger-border bg-danger-soft text-danger"
                  : "border-accent-border bg-accent-tint text-accent-deep"
              }`}
            >
              {reason.text}
            </p>
          )}
          {company ? (
            <>
              <a
                href={signInHref}
                className="mt-6 inline-flex h-11 w-full items-center justify-center rounded-full bg-accent px-6 text-[13px] font-semibold uppercase tracking-[0.06em] text-on-accent transition hover:bg-accent-deep"
              >
                Sign in with company email
              </a>
              <p className="mt-2 text-center text-xs text-muted">
                Opens {workforceSignInHost()} and sends a one-time code to your @adventure.asia inbox.
              </p>
            </>
          ) : registeredHost ? (
            <p role="alert" className="mt-6 rounded-md border border-danger-border bg-danger-soft px-3 py-2 text-[13px] text-danger">
              Company e-mail sign-in is not set up on this build. Point this address at a build made after the sign-in
              settings were added (root runbook preview-staff-sign-in).
            </p>
          ) : (
            <p role="status" className="mt-6 rounded-md border border-accent-border bg-accent-tint px-3 py-2 text-[13px] text-accent-deep">
              Staff sign-in on previews: {new URL(PREVIEW_ORIGIN).host}. Open the preview there (the PR label
              preview-signin points it at a PR).
            </p>
          )}
        </div>
        <p className="mt-5 text-center text-xs text-muted">
          Your account and Booking role are managed in{" "}
          <a
            href={CIS_STAFF_URL}
            target="_blank"
            rel="noopener noreferrer"
            className="font-medium text-accent-deep hover:underline"
          >
            CIS → Platform → Staff
          </a>
          .
        </p>
      </div>
    </main>
  );
}
