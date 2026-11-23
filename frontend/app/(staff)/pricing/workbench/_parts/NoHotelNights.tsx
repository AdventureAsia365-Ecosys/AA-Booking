"use client";
// app/(staff)/pricing/workbench/_parts/NoHotelNights.tsx — the can-operate check inside the components drawer
// (AA-1149, B13). GET …/operability shows whether agencies may buy the tour (Ready, Warnings, Blocked, Exception) and
// why; per night a "No hotel needed" box (a night train, a cruise not modelled as a room type). A marked night lifts
// rule N1 for good (no expiry, unlike the administrator's sale exception), so a change is saved with a reason of at
// least 20 characters: PUT …/no-hotel-nights (trip_plan.write) stores it on the tour and in the audit log, and the
// last reason is shown. A cruise with a cabin is better a hotel room type: then the night counts as covered. Kept apart
// from ComponentsDrawer.tsx (complexity limit).

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { operabilityApi, type Operability } from "@/lib/api/operability";
import { Button, Checkbox, Pill, Textarea, useToast } from "@/ui";
import { useT } from "@/ui/core/useI18n";
import { useTx } from "@/ui/local/useTx";
import { OPERABLE_META } from "../../readiness/_parts/OperableCell";
import { findingText } from "../../readiness/_parts/operableText";
import { MSG } from "./NoHotelNights.messages";

const REASON_MIN = 20;

function sameNights(a: number[], b: number[]): boolean {
  return a.length === b.length && [...a].sort().every((n, i) => n === [...b].sort()[i]);
}

export function NoHotelNights({ sourceId, canEdit }: { sourceId: string; canEdit: boolean }) {
  const t = useT(MSG);
  const tx = useTx();
  const key = ["operability", sourceId];
  const q = useQuery({ queryKey: key, queryFn: () => operabilityApi.get(sourceId) });
  const data = q.data;
  if (!data) return null;
  const meta = OPERABLE_META[data.status];
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
      {data.days > 1 && <NightsForm key={data.no_hotel_nights.join(",")} data={data} sourceId={sourceId} canEdit={canEdit} />}
      <p className="text-xs text-muted">{t("A night train or a cruise without a cabin room type needs no hotel; a cruise with a cabin is better added as a hotel room type.")}</p>
    </section>
  );
}

function NightsForm({ data, sourceId, canEdit }: { data: Operability; sourceId: string; canEdit: boolean }) {
  const t = useT(MSG);
  const qc = useQueryClient();
  const toast = useToast();
  const [draft, setDraft] = useState<number[]>(data.no_hotel_nights);
  const [reason, setReason] = useState("");
  const save = useMutation({
    mutationFn: () => operabilityApi.noHotelNights(sourceId, draft, reason.trim()),
    onSuccess: (next: Operability) => {
      qc.setQueryData(["operability", sourceId], next);
      qc.invalidateQueries({ queryKey: ["price-readiness"] });
      toast.success(t("Saved"));
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : t("Could not save")),
  });
  const nights = Array.from({ length: data.days - 1 }, (_, i) => i + 1);
  const changed = !sameNights(draft, data.no_hotel_nights);
  const short = reason.trim().length < REASON_MIN;
  return (
    <div className="flex flex-col gap-2 text-[13px]">
      <div className="flex flex-wrap gap-x-4 gap-y-1">
        {nights.map((n) => (
          <Checkbox
            key={n}
            label={t("Night {0}: no hotel needed", n)}
            checked={draft.includes(n)}
            disabled={!canEdit || save.isPending}
            onChange={(e) => setDraft((d) => (e.target.checked ? [...d, n] : d.filter((x) => x !== n)))}
          />
        ))}
      </div>
      {data.no_hotel_nights_reason && !changed && (
        <p className="text-xs text-muted">
          {t("Why")}: {data.no_hotel_nights_reason}
        </p>
      )}
      {changed && (
        <label className="flex flex-col gap-1">
          <span className="text-xs font-semibold text-muted">{t("Why do these nights need no hotel?")}</span>
          <Textarea className="min-h-[70px] w-full" maxLength={500} value={reason} onChange={(e) => setReason(e.target.value)} />
          <span className={`text-xs ${short ? "text-danger" : "text-muted"}`}>{t("{0} / at least {1} characters", reason.trim().length, REASON_MIN)}</span>
          <Button size="sm" variant="primary" className="self-start" disabled={short || save.isPending} onClick={() => save.mutate()}>
            {t("Save nights")}
          </Button>
        </label>
      )}
    </div>
  );
}
