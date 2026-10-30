// ui/local/StandaloneFrame.tsx
// AA-994 S6d — the <main> of the few pages drawn outside the staff shell (sign-in, the session notices, the 404): a
// full-screen centred frame. Inside the shell every page renders the kit PageFrame (lint rule no-raw-main); these pages
// have no shell, so the shell layer (ui/local, outside the kit lint scope, like AdminShell) owns their frame too.
// A server component: no hooks, no client code, so the sign-in page stays server-rendered.

import type { ReactNode } from "react";

export function StandaloneFrame({
  children,
  className = "",
  theme,
}: {
  children: ReactNode;
  /** Background and padding of the screen (e.g. "bg-bg px-4 py-10"); the frame adds the full-height centring. */
  className?: string;
  /** Pins a theme for this screen (the sign-in page is always light). */
  theme?: "light" | "dark";
}) {
  return (
    <main data-theme={theme} className={`grid min-h-screen place-items-center ${className}`}>
      {children}
    </main>
  );
}
