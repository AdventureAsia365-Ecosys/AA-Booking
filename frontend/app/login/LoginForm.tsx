"use client";
// app/login/LoginForm.tsx
// AA-683 — the LEGACY sign-in form (CIS username + password → the CIS bridge, until AA-680 PR-4). Posts
// {username, password} to the BFF (never to CIS from the browser); on success goes to `next` (or the dashboard).
// AA-680: the company e-mail button on the page is the primary sign-in; the Google placeholder is gone (staff never
// sign in with Google, ADR 0006a A9). The "session ended" notice is rendered by the page now.

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Eye, EyeOff, Loader2 } from "lucide-react";
import { safeNext } from "@/lib/safeNext";
import { Button, Input } from "@/ui";

const inputCls = "w-full";
// The sign-in card keeps its taller boxes (44 px, as before the kit controls); the password box leaves room for the
// show / hide button.
const TALL = { height: 44 } as const;

export function LoginForm({ next }: { next: string }) {
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
      {error && (
        <p role="alert" className="rounded-md border border-danger-border bg-danger-soft px-3 py-2 text-[13px] text-danger">
          {error}
        </p>
      )}
      <label className="flex flex-col gap-1.5">
        <span className="text-[13px] font-medium text-ink">CIS username</span>
        <Input
          className={inputCls}
          style={TALL}
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
          <Input
            className={inputCls}
            style={{ ...TALL, paddingRight: 44 }}
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
      <Button type="submit" variant="primary" size="lg" disabled={busy} style={{ marginTop: 4, height: 44 }}>
        {busy && <Loader2 size={16} className="animate-spin" />}
        {busy ? "Signing in…" : "Sign in"}
      </Button>
    </form>
  );
}
