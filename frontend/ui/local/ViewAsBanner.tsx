"use client";
// ui/local/ViewAsBanner.tsx — AA-1157: the banner of the owner's read-only "view as a lower role", in the shell's banner
// slot while the profile carries `view_as`. It names the role, says read-only, counts the minutes down, and "Back" ends
// the view (DELETE /api/view-as, as the owner) and reloads the dashboard with the same sign-in. A Booking-local file
// (never ui/core).

import { useEffect, useState } from "react";
import { Eye } from "lucide-react";
import { Button } from "../core/primitives";
import { useT } from "@/ui/core/useI18n";
import { VIEW_AS_API, minutesLeft } from "@/lib/viewAs";
import { useStaff } from "./StaffContext";
import { VIEW_AS_MSG } from "./ViewAsBanner.messages";

export function ViewAsBanner() {
  const t = useT(VIEW_AS_MSG);
  const view = useStaff().view_as;
  const [now, setNow] = useState(() => Date.now());
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!view) return;
    const tick = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(tick);
  }, [view]);

  if (!view) return null;
  const back = async () => {
    setBusy(true);
    try {
      await fetch(VIEW_AS_API, { method: "DELETE" });
    } catch {
      /* the cookie expires with the view */
    }
    window.location.assign("/roles");
  };
  return (
    <div
      role="status"
      data-testid="view-as-banner"
      className="flex flex-wrap items-center gap-x-4 gap-y-1.5 border-b border-line bg-warning-soft px-4 py-2 text-[13px] text-ink"
    >
      <span className="inline-flex items-center gap-1.5 font-semibold">
        <Eye size={14} /> {t("bannerTitle", view.target_label)}
      </span>
      <span className="min-w-0 text-muted">{t("bannerReadOnly")}</span>
      <span className="text-muted">{t("bannerEndsIn", minutesLeft(view.expires_at, now))}</span>
      <span className="ml-auto">
        <Button variant="secondary" size="sm" onClick={back} disabled={busy}>
          {busy ? t("backing") : t("back")}
        </Button>
      </span>
    </div>
  );
}
