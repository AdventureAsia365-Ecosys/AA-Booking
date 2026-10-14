"use client";
// app/login/LoginForm.tsx
// AA-683 — the sign-in form. Posts {username, password} to the BFF (never to CIS from the browser);
// on success goes to `next` (or the dashboard). The Google button renders only when
// NEXT_PUBLIC_GOOGLE_SIGNIN_URL is set (AA-703 delivers the flow).

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Eye, EyeOff, Loader2 } from "lucide-react";
import { GoogleMark } from "@/ui/GoogleMark";
import { safeNext } from "@/lib/safeNext";

const inputCls =
  "h-11 w-full rounded-md border border-line bg-card px-3.5 text-sm text-ink outline-none transition focus:border-accent-border focus:ring-2 focus:ring-accent-soft";

export function LoginForm({ next, expired, googleUrl }: { next: string; expired: boolean; googleUrl: string }) {
  const router = useRouter();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [show, setShow] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    if (!username.trim() || !password) {
      setError("Enter your CIS username and password.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const res = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: username.trim(), password }),
      });
      const body = (await res.json().catch(() => ({}))) as { ok?: boolean; message?: string };
      if (!res.ok || !body.ok) {
        setError(body.message || "Sign-in failed. Try again.");
        setBusy(false);
        return;
      }
      // Checked again here: the prop is server-validated, but this is the line that navigates.
      router.replace(safeNext(next));
      router.refresh();
    } catch {
      setError("The sign-in service is not answering. Check your connection and try again.");
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="mt-6 flex flex-col gap-4" noValidate>
      {expired && !error && (
        <p role="status" className="rounded-md border border-accent-border bg-accent-tint px-3 py-2 text-[13px] text-accent-deep">
          Your session has ended. Sign in again to continue.
        </p>
      )}
      {error && (
        <p role="alert" className="rounded-md border border-danger-border bg-danger-soft px-3 py-2 text-[13px] text-danger">
          {error}
        </p>
      )}
      <label className="flex flex-col gap-1.5">
        <span className="text-[13px] font-medium text-ink">CIS username</span>
        <input
          className={inputCls}
          name="username"
          autoComplete="username"
          autoCapitalize="none"
          spellCheck={false}
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          disabled={busy}
          required
        />
      </label>
      <label className="flex flex-col gap-1.5">
        <span className="text-[13px] font-medium text-ink">Password</span>
        <span className="relative">
          <input
            className={`${inputCls} pr-11`}
            name="password"
            type={show ? "text" : "password"}
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            disabled={busy}
            required
          />
          <button
            type="button"
            onClick={() => setShow((v) => !v)}
            aria-label={show ? "Hide password" : "Show password"}
            className="absolute right-1.5 top-1/2 grid h-8 w-8 -translate-y-1/2 cursor-pointer place-items-center rounded-md text-muted hover:text-ink"
          >
            {show ? <EyeOff size={16} /> : <Eye size={16} />}
          </button>
        </span>
      </label>
      <button
        type="submit"
        disabled={busy}
        className="mt-1 inline-flex h-11 cursor-pointer items-center justify-center gap-2 rounded-full bg-accent px-6 text-[13px] font-semibold uppercase tracking-[0.06em] text-on-accent transition hover:bg-accent-deep disabled:cursor-not-allowed disabled:opacity-60"
      >
        {busy && <Loader2 size={16} className="animate-spin" />}
        {busy ? "Signing in…" : "Sign in"}
      </button>
      {googleUrl && (
        <>
          <div className="flex items-center gap-3 text-[11px] uppercase tracking-[0.12em] text-muted2">
            <span className="h-px flex-1 bg-line" />
            or
            <span className="h-px flex-1 bg-line" />
          </div>
          <a
            href={googleUrl}
            className="inline-flex h-11 items-center justify-center gap-2.5 rounded-full border border-line bg-card px-6 text-[13px] font-semibold text-ink transition hover:bg-bg"
          >
            <GoogleMark />
            Continue with Google
          </a>
        </>
      )}
    </form>
  );
}
