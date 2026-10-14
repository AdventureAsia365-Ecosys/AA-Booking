// ui/signOut.ts
// AA-683 — sign out: the BFF clears both httpOnly cookies, then the app goes to /login.

type Router = { replace: (href: string) => void; refresh: () => void };

export async function signOut(router: Router): Promise<void> {
  try {
    await fetch("/api/auth/logout", { method: "POST" });
  } catch {
    // Redirect either way: without the cookies the proxy sends every page to /login.
  }
  router.replace("/login");
  router.refresh();
}
