"use client";
// app/(staff)/suppliers/_parts/RateReview.tsx — AA-1153 (T10): the business checks of a supplier's rates (a board with
// more meals priced below one with fewer, season gaps and overlaps, price jumps against the previous contract) and the
// diff against that previous contract (added / changed / removed rates). Shown in the rate-sheet check and, live, in
// the publish dialog of a draft (ContractPublishReview), where an imported draft with warnings needs the publisher to
// tick "I checked these warnings".

import { useQuery } from "@tanstack/react-query";
import { suppliersApi, type RateCheck, type RateDiff, type RateSheetResult } from "@/lib/api/suppliers";
import { Checkbox, Pill, Skeleton } from "@/ui";
import { useT } from "@/ui/core/useI18n";
import { useTx } from "@/ui/local/useTx";
import { MSG } from "./RateReview.messages";

const LISTED = 8;

export function RateChecks({ checks }: { checks: RateCheck[] }) {
  const t = useT(MSG);
  const tx = useTx();
  if (!checks.length) return <p className="text-xs text-muted">{t("No business check found anything to look at.")}</p>;
  return (
    <ul className="flex flex-col gap-1 rounded-md border border-line p-3 text-xs text-body" aria-label={t("Business checks")} data-testid="rate-checks">
      {checks.map((c, i) => (
        <li key={i} className="flex flex-wrap items-center gap-1.5">
          <Pill tone={c.severity === "error" ? "danger" : "warning"} dot={false}>
            {c.severity === "error" ? t("Error") : t("Warning")}
          </Pill>
          <span className="min-w-0 break-words">
            {c.sheet ? `${c.sheet}${c.row ? t(" row {0}", c.row) : ""}: ` : ""}
            {tx(c.message)}
          </span>
        </li>
      ))}
    </ul>
  );
}

export function RateDiffSummary({ diff }: { diff: RateDiff }) {
  const t = useT(MSG);
  const tx = useTx();
  const row = (r: RateDiff["added"][number]) =>
    [r.label, r.meal_plan, r.season ?? (r.valid_from ? `${r.valid_from} – ${r.valid_to ?? "…"}` : null)].filter(Boolean).join(" · ");
  return (
    <div className="flex flex-col gap-1 rounded-md border border-line p-3 text-xs text-body" data-testid="rate-diff">
      <p className="font-medium text-ink">
        {diff.previous_contract
          ? t("Against {0}: {1} added, {2} changed, {3} removed", tx(diff.previous_contract.title), diff.added.length, diff.changed.length, diff.removed.length)
          : t("No previous contract of this supplier: every rate is new ({0}).", diff.added.length)}
      </p>
      {diff.changed.length > 0 && (
        <ul className="flex flex-col gap-0.5 text-muted">
          {diff.changed.slice(0, LISTED).map((r, i) => (
            <li key={i}>
              {tx(row(r))}: {r.old_net} → {r.new_net}
              {r.change_pct ? ` (${Number(r.change_pct) > 0 ? "+" : ""}${r.change_pct} %)` : ""}
            </li>
          ))}
          {diff.changed.length > LISTED && <li>{t("… and {0} more", diff.changed.length - LISTED)}</li>}
        </ul>
      )}
      {diff.removed.length > 0 && (
        <p className="text-muted">
          {t("Not in this contract any more:")} {diff.removed.slice(0, LISTED).map((r) => tx(row(r))).join("; ")}
        </p>
      )}
    </div>
  );
}

/** AA-1153: the checks and diff of a rate-sheet Check (dry run), when the API sent them. */
export function SheetReview({ preview }: { preview: RateSheetResult | null }) {
  if (!preview) return null;
  return (
    <>
      {preview.checks && <RateChecks checks={preview.checks} />}
      {preview.diff && <RateDiffSummary diff={preview.diff} />}
    </>
  );
}

/** The live review of a draft in the publish dialog; ``onAcknowledge`` is told whether publishing may go ahead. */
export function ContractPublishReview({
  contract,
  acknowledged,
  onAcknowledge,
}: {
  contract: { id: string } | null | undefined;
  acknowledged: boolean;
  onAcknowledge: (value: boolean) => void;
}) {
  const t = useT(MSG);
  const contractId = contract?.id;
  const q = useQuery({
    queryKey: ["contract-review", contractId],
    queryFn: () => suppliersApi.review(contractId ?? ""),
    enabled: !!contractId,
  });
  if (!contractId) return null;
  if (q.isLoading) return <Skeleton h={60} />;
  if (q.isError || !q.data) return <p className="text-xs text-muted">{t("The rate review could not be loaded.")}</p>;
  const r = q.data;
  return (
    <div className="mt-3 flex flex-col gap-2">
      <RateDiffSummary diff={r.diff} />
      {r.checks.length > 0 && <RateChecks checks={r.checks} />}
      {r.acknowledge && (
        <Checkbox
          checked={acknowledged}
          onChange={(e) => onAcknowledge(e.target.checked)}
          label={t("I checked these warnings with the supplier's sheet.")}
        />
      )}
    </div>
  );
}
