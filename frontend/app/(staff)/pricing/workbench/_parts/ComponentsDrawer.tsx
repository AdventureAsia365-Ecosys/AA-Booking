"use client";
// app/(staff)/pricing/workbench/_parts/ComponentsDrawer.tsx — AA-1091 item 2: a tour's hotels and services, with the
// ones suggested from the CIS itinerary (GET …/component-suggestions: per day the hotel of the night and the services
// the day's text mentions, matched to existing HTL- / SVC- codes with the reasons of the match). Staff accept, change
// (another match, another room type) or reject each suggestion and remove current items; nothing is written until
// Save, which sends the plan's whole set (PUT …/components, ui/booking/priceWorkbench.ts mergeComponents). Unmatched
// needs link to the supplier, hotel and service pages so the missing item can be created. Opened from Price setup and
// from the trip plan's Components tab; the text follows the page's language (messages.ts; English outside a provider).
// AA-1091: on Price setup it shows the tour's tabs (Hotels & services | Versions, VersionsDrawer.tsx).

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { Check, Undo2, X } from "lucide-react";
import {
  priceWorkbenchApi,
  type ComponentSuggestions,
  type PlanComponent,
  type SuggestionCandidate,
  type SuggestionNeed,
} from "@/lib/api/priceWorkbench";
import { mergeComponents, type ItemRef } from "@/ui/booking/priceWorkbench";
import { Button, Drawer, Pill, Select, useCan, useT, useToast } from "@/ui";
import { M, type MessageKey } from "./messages";
import { PlanTabs } from "./VersionsDrawer";

type Choice = { index: number; roomTypeId?: string };

function needId(day: number, need: SuggestionNeed): string {
  return `${day}:${need.kind}`;
}

function itemOf(day: number, cand: SuggestionCandidate, roomTypeId?: string): ItemRef {
  return cand.hotel_room_type_id
    ? { day_no: day, hotel_room_type_id: roomTypeId ?? cand.hotel_room_type_id }
    : { day_no: day, service_id: cand.service_id };
}

function currentRef(c: PlanComponent): ItemRef {
  return { day_no: c.day_no, hotel_room_type_id: c.hotel_room_type_id, service_id: c.service_id };
}

function refKey(r: ItemRef): string {
  return `${r.day_no}|${r.hotel_room_type_id ?? ""}|${r.service_id ?? ""}`;
}

const CONFIDENCE_TONE = { high: "success", medium: "info", low: "neutral" } as const;

/** ``obj`` without ``key`` (the toggles of the drawer's state maps). */
function without<V>(obj: Record<string, V>, key: string): Record<string, V> {
  const out = { ...obj };
  delete out[key];
  return out;
}

function toggle<V>(obj: Record<string, V>, key: string, value: V): Record<string, V> {
  return key in obj ? without(obj, key) : { ...obj, [key]: value };
}

function CandidateCard({
  need,
  choice,
  isAccepted,
  isRejected,
  canEdit,
  onChoose,
  onAccept,
  onReject,
}: {
  need: SuggestionNeed;
  choice: Choice;
  isAccepted: boolean;
  isRejected: boolean;
  canEdit: boolean;
  onChoose: (next: Choice) => void;
  onAccept: (pick: Choice) => void;
  onReject: () => void;
}) {
  const t = useT(M);
  const cands = need.suggestion ? [need.suggestion, ...need.alternatives] : [];
  const cand = cands[choice.index] ?? cands[0];
  if (!cand) return null;
  const roomId = choice.roomTypeId ?? cand.hotel_room_type_id ?? undefined;
  return (
    <div
      className={`flex min-w-0 flex-col gap-2 rounded-md border border-line p-3 ${isRejected ? "opacity-60" : ""}`}
      data-testid="component-suggestion"
    >
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <Pill tone="accent" dot={false}>
          {t(`kind_${need.kind}` as MessageKey) as string}
        </Pill>
        <span className="min-w-0 break-words text-[13px] font-medium text-ink">{cand.label}</span>
        <Pill tone={CONFIDENCE_TONE[cand.confidence]}>{t(`confidence_${cand.confidence}` as MessageKey) as string}</Pill>
      </div>
      {cands.length > 1 && (
        <Select
          size="sm"
          aria-label={t("compAlternative")}
          value={String(choice.index)}
          onChange={(e) => onChoose({ index: Number(e.target.value) })}
          options={cands.map((c, i) => ({ value: String(i), label: `${c.label} (${Math.round(c.score * 100)})` }))}
        />
      )}
      {cand.room_types.length > 1 && (
        <Select
          size="sm"
          aria-label={t("compRoomType")}
          value={roomId}
          onChange={(e) => onChoose({ index: choice.index, roomTypeId: e.target.value })}
          options={cand.room_types.map((r) => ({ value: r.id, label: r.name }))}
        />
      )}
      <ul className="flex list-disc flex-col gap-0.5 pl-4 text-xs text-muted" aria-label={t("compWhy")}>
        {cand.reasons.map((r) => (
          <li key={r}>{r}</li>
        ))}
      </ul>
      {canEdit && (
        <div className="flex flex-wrap gap-2">
          <Button
            size="sm"
            variant={isAccepted ? "primary" : "secondary"}
            aria-pressed={isAccepted}
            onClick={() => onAccept({ index: choice.index, roomTypeId: roomId })}
          >
            <Check size={13} /> {isAccepted ? t("compAccepted") : t("compAccept")}
          </Button>
          <Button size="sm" variant="ghost" aria-pressed={isRejected} onClick={onReject}>
            <X size={13} /> {isRejected ? t("compRejected") : t("compReject")}
          </Button>
        </div>
      )}
    </div>
  );
}

export function ComponentsDrawer({
  sourceId,
  title,
  onClose,
  onVersions,
}: {
  sourceId: string;
  title?: string | null;
  onClose: () => void;
  /** AA-1091: on Price setup, the tour's Versions tab (the tabs show only when given). */
  onVersions?: () => void;
}) {
  const t = useT(M);
  const toast = useToast();
  const qc = useQueryClient();
  const canEdit = useCan("trip_plan.write");
  const [accepted, setAccepted] = useState<Record<string, Choice>>({});
  const [rejected, setRejected] = useState<Record<string, true>>({});
  const [choices, setChoices] = useState<Record<string, Choice>>({});
  const [removed, setRemoved] = useState<Record<string, true>>({});

  const current = useQuery({ queryKey: ["plan-components", sourceId], queryFn: () => priceWorkbenchApi.components(sourceId) });
  const suggestions = useQuery({
    queryKey: ["component-suggestions", sourceId],
    queryFn: () => priceWorkbenchApi.suggestions(sourceId),
  });
  const kindLabel = (kind: string) => t(`kind_${kind}` as MessageKey) as string;

  function acceptedItems(data: ComponentSuggestions): ItemRef[] {
    const out: ItemRef[] = [];
    for (const day of data.days) {
      for (const need of day.needs) {
        const pick = accepted[needId(day.day_no, need)];
        const cands = need.suggestion ? [need.suggestion, ...need.alternatives] : [];
        if (pick && cands[pick.index]) out.push(itemOf(day.day_no, cands[pick.index], pick.roomTypeId));
      }
    }
    return out;
  }

  const save = useMutation({
    mutationFn: async () => {
      const items = current.data?.items ?? [];
      const add = suggestions.data ? acceptedItems(suggestions.data) : [];
      const gone = items.filter((c) => removed[refKey(currentRef(c))]).map(currentRef);
      if (!add.length && !gone.length) throw new Error(t("compNothing"));
      return priceWorkbenchApi.saveComponents(sourceId, mergeComponents(items.map(currentRef), add, gone));
    },
    onSuccess: () => {
      toast.success(t("compSaved"));
      for (const key of [["plan-components", sourceId], ["component-suggestions", sourceId], ["price-workbench"], ["price-readiness"]]) {
        qc.invalidateQueries({ queryKey: key });
      }
      onClose();
    },
  });

  const dirty = Object.keys(accepted).length > 0 || Object.keys(removed).length > 0;
  function close() {
    if (save.isPending) return;
    if (dirty && !window.confirm(t("discard"))) return;
    onClose();
  }
  const changes = Object.keys(accepted).length + Object.keys(removed).length;

  const data = suggestions.data;
  const items = current.data?.items ?? [];
  return (
    <Drawer
      open
      onClose={close}
      title={title ? `${t("compTitle")} · ${title}` : t("compTitle")}
      width={640}
      footer={
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={close} disabled={save.isPending}>
            {t("cancel")}
          </Button>
          {canEdit && (
            <Button variant="primary" onClick={() => save.mutate()} disabled={save.isPending || !changes}>
              {save.isPending ? t("saving") : t("compSave", items.length - Object.keys(removed).length + Object.keys(accepted).length)}
            </Button>
          )}
        </div>
      }
    >
      <div className="flex min-w-0 flex-col gap-4" data-testid="components-drawer">
        {onVersions && <PlanTabs active="components" onTab={(tab) => tab === "versions" && onVersions()} />}
        <p className="text-[13px] text-muted">{t("compIntro")}</p>
        {save.error && (
          <p className="rounded-md border border-danger-border bg-danger-soft px-3 py-2 text-[13px] text-body" role="alert">
            {(save.error as Error).message}
          </p>
        )}
        <section className="flex flex-col gap-2">
          <h3 className="text-[13px] font-semibold text-ink">{t("compCurrent")}</h3>
          {!items.length && <p className="text-[13px] text-muted">{t("compNone")}</p>}
          <ul className="flex flex-col gap-1.5">
            {items.map((c) => {
              const k = refKey(currentRef(c));
              const gone = !!removed[k];
              return (
                <li key={c.component_id + c.day_no} className="flex min-w-0 items-center justify-between gap-2 text-[13px]">
                  <span className={`min-w-0 break-words ${gone ? "text-muted line-through" : "text-body"}`}>
                    {t("compDay", c.day_no)} · {c.label}
                  </span>
                  {canEdit && (
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => setRemoved((r) => toggle(r, k, true as const))}
                    >
                      {gone ? <Undo2 size={13} /> : <X size={13} />} {gone ? t("compUndo") : t("compRemove")}
                    </Button>
                  )}
                </li>
              );
            })}
          </ul>
        </section>
        {suggestions.isLoading && <p className="text-[13px] text-muted">{t("compLoading")}</p>}
        {suggestions.isError && (
          <p className="text-[13px] text-body" role="alert">
            {(suggestions.error as Error).message}
          </p>
        )}
        {!!data?.degraded.length && (
          <p className="rounded-md border border-accent-border bg-accent-tint px-3 py-2 text-[13px] text-body" role="status">
            {t("compDegraded")}
          </p>
        )}
        {data?.days.map((day) => (
          <section key={day.day_no} className="flex min-w-0 flex-col gap-2 border-t border-line pt-3">
            <h3 className="min-w-0 break-words text-[13px] font-semibold text-ink">
              {t("compDay", day.day_no)}
              {day.title ? ` · ${day.title}` : ""}
            </h3>
            {day.overnight_place && <p className="text-xs text-muted">{t("compNight", day.overnight_place)}</p>}
            {day.needs.map((need) =>
              need.status === "suggested" ? (
                <CandidateCard
                  key={need.kind}
                  need={need}
                  choice={choices[needId(day.day_no, need)] ?? { index: 0 }}
                  isAccepted={!!accepted[needId(day.day_no, need)]}
                  isRejected={!!rejected[needId(day.day_no, need)]}
                  canEdit={canEdit}
                  onChoose={(next) => {
                    const id = needId(day.day_no, need);
                    setChoices((c) => ({ ...c, [id]: next }));
                    setAccepted((a) => (id in a ? { ...a, [id]: next } : a));
                  }}
                  onAccept={(pick) => {
                    const id = needId(day.day_no, need);
                    setRejected((r) => without(r, id));
                    setAccepted((a) => toggle(a, id, pick));
                  }}
                  onReject={() => {
                    const id = needId(day.day_no, need);
                    setAccepted((a) => without(a, id));
                    setRejected((r) => toggle(r, id, true as const));
                  }}
                />
              ) : need.status === "covered" ? (
                <p key={need.kind} className="min-w-0 break-words text-xs text-muted">
                  {kindLabel(need.kind)} · {t("compCovered")}: {need.current.map((c) => c.label).join(", ")}
                </p>
              ) : (
                <div key={need.kind} className="flex min-w-0 flex-wrap items-center gap-2 text-xs text-body">
                  <Pill tone="warning">{t("compUnmatched")}</Pill>
                  <span className="min-w-0 break-words">
                    {kindLabel(need.kind)}
                    {need.keywords.length ? ` (${need.keywords.join(", ")})` : need.place ? ` (${need.place})` : ""}
                  </span>
                  <Link href="/suppliers/new" className="font-semibold text-accent-deep hover:underline">
                    {t("compCreateSupplier")}
                  </Link>
                  <Link href={need.kind === "hotel" ? "/hotels" : "/services"} className="font-semibold text-accent-deep hover:underline">
                    {need.kind === "hotel" ? t("compCreateHotel") : t("compCreateService")}
                  </Link>
                </div>
              ),
            )}
          </section>
        ))}
      </div>
    </Drawer>
  );
}
