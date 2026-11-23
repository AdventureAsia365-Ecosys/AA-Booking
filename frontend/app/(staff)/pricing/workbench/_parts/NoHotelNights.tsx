"use client";
// app/(staff)/pricing/workbench/_parts/NoHotelNights.tsx — the can-operate check inside the components drawer
// (AA-1149, B13). GET …/operability shows whether agencies may buy the tour (Ready, Warnings, Blocked, Exception) and
// why; per night a "No hotel needed" box (a night train, a cruise not modelled as a room type) saved at once with
// PUT …/no-hotel-nights (trip_plan.write). A cruise with a cabin is better a hotel room type: then the night counts as
// covered. Kept apart from ComponentsDrawer.tsx (complexity limit).

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { operabilityApi, type Operability } from "@/lib/api/operability";
import { Checkbox, Pill, useToast } from "@/ui";
import { useT } from "@/ui/core/useI18n";
import { useTx } from "@/ui/local/useTx";
import { OPERABLE_META } from "../../readiness/_parts/OperableCell";
import { findingText } from "../../readiness/_parts/operableText";
import { MSG } from "./NoHotelNights.messages";

export function NoHotelNights({ sourceId, canEdit }: { sourceId: string; canEdit: boolean }) {
  const t = useT(MSG);
  const tx = useTx();
  const qc = useQueryClient();
  const toast = useToast();
  const key = ["operability", sourceId];
  const q = useQuery({ queryKey: key, queryFn: () => operabilityApi.get(sourceId) });
  const save = useMutation({
    mutationFn: (nights: number[]) => operabilityApi.noHotelNights(sourceId, nights),
    onSuccess: (next: Operability) => {
      qc.setQueryData(key, next);
      qc.invalidateQueries({ queryKey: ["price-readiness"] });
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : t("Could not save")),
  });
  const data = q.data;
  if (!data) return null;
  const meta = OPERABLE_META[data.status];
  const nights = Array.from({ length: Math.max(data.days - 1, 0) }, (_, i) => i + 1);
  const toggle = (n: number, on: boolean) =>
    save.mutate(on ? [...data.no_hotel_nights, n] : data.no_hotel_nights.filter((x) => x !== n));
  return (
    <section className="flex flex-col gap-2 border-t border-line pt-3" data-testid="no-hotel-nights">
      <h3 className="flex flex-wrap items-center gap-2 text-[13px] font-semibold text-ink">
        {t("Can agencies buy it?")} <Pill tone={meta.tone} dot={false}>{tx(meta.label)}</Pill>
      </h3>
      <ul className="flex flex-col gap-0.5 text-xs">
        {data.findings.map((f) => (
          <li key={f.key} className={f.level === "block" ? "text-body" : "text-muted"}>
            {findingText(f, (k, v) => t(k, v))} ({f.rule})
          </li>
        ))}
      </ul>
      {nights.length > 0 && (
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-[13px]">
          {nights.map((n) => (
            <Checkbox
              key={n}
              label={t("Night {0}: no hotel needed", n)}
              checked={data.no_hotel_nights.includes(n)}
              disabled={!canEdit || save.isPending}
              onChange={(e) => toggle(n, e.target.checked)}
            />
          ))}
        </div>
      )}
      <p className="text-xs text-muted">{t("A night train or a cruise without a cabin room type needs no hotel; a cruise with a cabin is better added as a hotel room type.")}</p>
    </section>
  );
}
