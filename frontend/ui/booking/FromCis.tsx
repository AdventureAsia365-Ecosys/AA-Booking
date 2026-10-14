// ui/booking/FromCis.tsx
// AA-683 — CIS-owned content is read-only in Booking (ADR 0004 O7, UI spec §7): a "From CIS" chip on
// every such block, and an "Edit in CIS" link for the roles that edit content there (admin, content_creator).

import { ExternalLink } from "lucide-react";
import { CIS_ADMIN_URL } from "@/lib/config";

export function FromCisTag() {
  return (
    <span className="inline-flex items-center rounded-full bg-neutral-bg px-2 py-0.5 text-[10.5px] font-semibold uppercase tracking-[0.06em] text-neutral-fg">
      From CIS
    </span>
  );
}

export function ManagedInCisLink({ path = "", label = "Edit in CIS" }: { path?: string; label?: string }) {
  return (
    <a
      href={`${CIS_ADMIN_URL}${path}`}
      target="_blank"
      rel="noopener noreferrer"
      className="inline-flex items-center gap-1 text-[12px] font-medium text-accent-deep hover:underline"
    >
      {label} <ExternalLink size={12} />
    </a>
  );
}
