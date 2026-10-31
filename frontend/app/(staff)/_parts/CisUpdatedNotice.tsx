"use client";
// app/(staff)/_parts/CisUpdatedNotice.tsx — AA-1013: a proposal version made before CIS rewrote its tour keeps the day
// titles copied then (still tagged "From CIS"). The builder and the preview say so, name the tour and when CIS last
// updated it, and the builder offers "Refresh from CIS" on a draft (copies today's titles into the draft; Save keeps
// them). Never automatic: a sent or approved version stays as the customer saw it.

import type { ReactNode } from "react";
import { RefreshCw } from "lucide-react";
import { DateText } from "@/ui";
import type { CisUpdateSummary } from "@/ui/booking/cisReadback";

export function CisUpdatedNotice({
  summary,
  hint,
  action,
}: {
  summary: CisUpdateSummary;
  /** What staff can do here (refresh, open the builder, make a new version). */
  hint: string;
  action?: ReactNode;
}) {
  if (summary.count === 0) return null;
  const titles = summary.count === 1 ? "1 item title differs" : `${summary.count} item titles differ`;
  return (
    <div
      role="status"
      data-testid="cis-updated-notice"
      className="flex flex-wrap items-start justify-between gap-3 rounded-lg border border-warning bg-warning-soft px-4 py-3 text-[13px] text-body"
    >
      <div className="flex min-w-0 flex-1 gap-2">
        <RefreshCw size={15} className="mt-0.5 shrink-0 text-warning" aria-hidden="true" />
        <div className="min-w-0">
          <p className="font-semibold text-ink">CIS has newer text for {summary.tours.length === 1 ? "this tour" : "these tours"}</p>
          <p className="mt-0.5 break-words">
            {titles} from what CIS publishes today
            {summary.tours.map((t, i) => (
              <span key={t.source_id}>
                {i === 0 ? ": " : "; "}
                <span className="font-medium text-ink">{t.title ?? t.source_id}</span>
                {t.updated_at && (
                  <>
                    {" "}
                    (updated <DateText value={t.updated_at} />)
                  </>
                )}
              </span>
            ))}
            .
          </p>
          <p className="mt-0.5 text-xs text-muted">{hint}</p>
        </div>
      </div>
      {action && <div className="shrink-0">{action}</div>}
    </div>
  );
}
