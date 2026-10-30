"use client";
// app/(staff)/SessionNotice.tsx
// AA-683 — full-page notice for a signed-in user who cannot enter the shell (pending activation, refused,
// or the Booking API is down). Always offers sign-out; "Try again" re-runs the server layout.

import { useRouter } from "next/navigation";
import { AlertTriangle, Hourglass, ShieldOff } from "lucide-react";
import { Button, LOGO_SRC, signOut } from "@/ui";
import { StandaloneFrame } from "@/ui/local/StandaloneFrame";

const ICON = {
  pending: <Hourglass size={28} className="text-accent" />,
  blocked: <ShieldOff size={28} className="text-danger" />,
  error: <AlertTriangle size={28} className="text-danger" />,
};

export function SessionNotice({
  tone,
  title,
  message,
  retry = false,
  link,
}: {
  tone: keyof typeof ICON;
  title: string;
  message: string;
  retry?: boolean;
  link?: { href: string; label: string };
}) {
  const router = useRouter();
  return (
    <StandaloneFrame className="bg-bg px-4 py-10">
      <div className="w-full max-w-[460px] rounded-lg border border-line bg-card p-8 text-center shadow-[0_20px_60px_rgba(0,0,0,0.08)]">
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img src={LOGO_SRC} alt="Adventure Asia" width={56} height={36} className="mx-auto mb-5 h-auto w-14" />
        <div className="mx-auto mb-3 grid h-12 w-12 place-items-center rounded-full bg-accent-tint">{ICON[tone]}</div>
        <h1 className="font-display text-xl font-semibold text-ink">{title}</h1>
        <p className="mt-2 text-sm text-muted">{message}</p>
        {link && (
          <a
            href={link.href}
            target="_blank"
            rel="noopener noreferrer"
            className="mt-3 inline-block text-sm font-medium text-accent-deep hover:underline"
          >
            {link.label}
          </a>
        )}
        <div className="mt-6 flex justify-center gap-2">
          {retry && (
            <Button variant="primary" onClick={() => router.refresh()}>
              Try again
            </Button>
          )}
          <Button variant="ghost" onClick={() => signOut(router)}>
            Sign out
          </Button>
        </div>
      </div>
    </StandaloneFrame>
  );
}
