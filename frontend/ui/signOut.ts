// ui/signOut.ts
// AA-683 / AA-680 — sign out: the BFF clears every session cookie (and revokes a Cognito refresh token). After a
// company e-mail session the browser goes through Cognito /logout (ends the managed-login session, then back to
// /login); otherwise straight to /login.

type Router = { replace: (href: string) => void; refresh: () => void };

export async function signOut(router: Router): Promise<void> {
  let logoutUrl: string | null = null;
  try {
    const res = await fetch("/api/auth/logout", { method: "POST" });
    const body = (await res.json().catch(() => ({}))) as { logoutUrl?: unknown };
    logoutUrl = typeof body.logoutUrl === "string" && body.logoutUrl.startsWith("https://") ? body.logoutUrl : null;
  } catch {
    // Redirect either way: without the cookies the proxy sends every page to /login.
  }
  if (logoutUrl) {
    window.location.assign(logoutUrl);
    return;
  }
  router.replace("/login");
  router.refresh();
}
