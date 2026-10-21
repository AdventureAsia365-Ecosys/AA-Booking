// app/login/page.tsx
// AA-683 / AA-680 — staff sign-in (UI spec §4.1). Visual: the adventure.asia look on the kit tokens: a cream-to-gold
// wash (the Figma "Group 1410084962" background, rebuilt in CSS), the Satisfy accent line (>= 32 px, design system
// §2.2), Fahkwang title, gold pill button. Pinned light, like the site and the CIS login (data-theme="light"
// re-scopes the --aa-* variables).
//
// AA-680 (root ADR 0006 / 0006a): the primary way in is "Sign in with company email" — the Cognito WORKFORCE
// managed login (booking-staff client, staff-auth.lumiguides.it.com, an e-mail code). It is offered only on the
// registered hosts with the client secret set (lib/cognito.ts). The legacy CIS username + password form (the bridge,
// until AA-680 PR-4) stays: collapsed under the company button where that button exists, and as the only form on
// hosts without company sign-in (per-deployment Vercel preview URLs). Booking roles are managed in CIS → Platform →
// Staff. AA-911: the fixed Preview host booking-preview.lumiguides.it.com never shows the legacy form.

import type { Metadata } from "next";
import { headers } from "next/headers";
import { LoginForm } from "./LoginForm";
import { CIS_STAFF_URL } from "@/lib/config";
import { cognitoSignInAvailable, isPreviewSignInHost, workforceSignInHost } from "@/lib/cognito";
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
    text: "Company e-mail sign-in is not available on this address. Use the CIS password form below.",
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
  // AA-911: no legacy password form on the fixed Preview host (a stable URL that may be public).
  const legacyForm = !isPreviewSignInHost(host);
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
            {company
              ? "Use your company e-mail. Trip cases, proposals and customers in one place."
              : "Use your CIS staff account. Trip cases, proposals and customers in one place."}
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
              {legacyForm && (
                <details className="mt-6 border-t border-line pt-4">
                  <summary className="cursor-pointer text-[13px] font-medium text-muted hover:text-ink">
                    Use a CIS username and password instead (legacy)
                  </summary>
                  <LoginForm next={next} />
                </details>
              )}
            </>
          ) : legacyForm ? (
            <LoginForm next={next} />
          ) : (
            <p role="alert" className="mt-6 rounded-md border border-danger-border bg-danger-soft px-3 py-2 text-[13px] text-danger">
              Company e-mail sign-in is not set up on this preview build. Point this address at a preview built after
              the sign-in settings were added (root runbook preview-staff-sign-in).
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
