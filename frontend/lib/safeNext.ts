// lib/safeNext.ts
// AA-683 — the post-login target (`/login?next=…`) must stay on this origin (S236 review finding 1).
//
// A prefix check alone is not enough: browsers strip tab / CR / LF inside a URL and treat "\" like "/", so
// "/\t/evil.com" or "/%09/evil.com" (decoded by Next before it reaches us) becomes "//evil.com", a
// protocol-relative URL to another host. So: refuse any control character or backslash, then resolve the
// value against a placeholder origin and keep it only when the origin is unchanged.

const PLACEHOLDER = "http://booking.invalid";
const UNSAFE = /[\u0000-\u001F\u007F\\]/;

export function safeNext(raw: unknown): string {
  const v = Array.isArray(raw) ? raw[0] : raw;
  if (typeof v !== "string" || !v.startsWith("/") || v.startsWith("//") || UNSAFE.test(v)) return "/";
  let url: URL;
  try {
    url = new URL(v, PLACEHOLDER);
  } catch {
    return "/";
  }
  if (url.origin !== PLACEHOLDER) return "/";
  if (url.pathname === "/login" || url.pathname.startsWith("/login/")) return "/";
  const out = `${url.pathname}${url.search}${url.hash}`;
  // Belt and braces: the normalised result must itself be a plain same-origin path.
  return out.startsWith("/") && !out.startsWith("//") ? out : "/";
}
