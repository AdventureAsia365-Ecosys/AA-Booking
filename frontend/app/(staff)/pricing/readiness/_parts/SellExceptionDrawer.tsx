"use client";
// app/(staff)/pricing/readiness/_parts/SellExceptionDrawer.tsx — the admin sale exception of a tour (AA-1149, B13).
// GET /booking/catalog/trip-plans/{key}/operability, then POST …/sell-exception or …/sell-exception/revoke
// (operable_exception.approve: administrator only). Only the failing rules that can be overridden are offered: a night
// without a hotel (N1) and a supplier not active (N3). A missing rate or an inactive room type, hotel or service can
// never be overridden (the server's sentence says so). The exception covers exactly today's findings of the chosen
// rules: a new gap blocks the tour again. Reason at least 20 characters; length in days, default and maximum from
// Pricing > Policy (section Operations). Trips sold under it get a trip check to book the gap by hand.

import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { ApiError } from "@/lib/api/client";
import { operabilityApi, type Operability } from "@/lib/api/operability";
import { Button, Checkbox, DateText, FormDrawer, Input, Textarea, useToast } from "@/ui";
import { useT } from "@/ui/core/useI18n";
import { MSG } from "./SellExceptionDrawer.messages";
import { findingText } from "./operableText";

const REASON_MIN = 20;
const RULE_LABEL = { N1: "A night without a hotel (N1)", N3: "A supplier not active (N3)" } as const;

function errorText(e: unknown): string | null {
  return e instanceof ApiError || e instanceof Error ? e.message : null;
}

export function SellExceptionDrawer({ tourKey, onClose, onChanged }: { tourKey: string; onClose: () => void; onChanged: () => void }) {
  const t = useT(MSG);
  const q = useQuery({ queryKey: ["operability", tourKey], queryFn: () => operabilityApi.get(tourKey) });
  return (
    <FormDrawer open onClose={onClose} title={t("Sale exception")} submitLabel={t("Close")} onSubmit={onClose}>
      {q.data ? (
        <ExceptionBody data={q.data} tourKey={tourKey} onChanged={onChanged} onClose={onClose} />
      ) : (
        <p className="text-[13px] text-muted">{q.isError ? errorText(q.error) : t("Loading…")}</p>
      )}
    </FormDrawer>
  );
}

function ExceptionBody({
  data,
  tourKey,
  onChanged,
  onClose,
}: {
  data: Operability;
  tourKey: string;
  onChanged: () => void;
  onClose: () => void;
}) {
  const t = useT(MSG);
  const toast = useToast();
  const rules = (Object.keys(data.overridable) as ("N1" | "N3")[]).filter((r) => data.overridable[r]?.length);
  const [chosen, setChosen] = useState<("N1" | "N3")[]>(rules);
  const [reason, setReason] = useState("");
  const [days, setDays] = useState(String(data.exception_default_days));
  const done = (message: string) => {
    toast.success(message);
    onChanged();
    onClose();
  };
  const allow = useMutation({
    mutationFn: () => operabilityApi.allow(tourKey, { rules: chosen, reason: reason.trim(), days: Number(days) }),
    onSuccess: () => done(t("Agencies can buy this tour until the exception ends")),
  });
  const revoke = useMutation({
    mutationFn: () => operabilityApi.revoke(tourKey, t("Revoked by the administrator")),
    onSuccess: () => done(t("Exception revoked")),
  });
  const length = Number(days);
  const ready = chosen.length > 0 && reason.trim().length >= REASON_MIN && length >= 1 && length <= data.exception_max_days;
  const blockers = data.findings.filter((f) => f.level === "block");
  return (
    <div className="flex flex-col gap-4 text-[13px]">
      <ul className="flex flex-col gap-1" aria-label={t("Why it cannot be sold")}>
        {blockers.map((f) => (
          <li key={f.key} className={f.covered ? "text-muted line-through" : "text-body"}>
            {findingText(f, (k, v) => t(k, v))} ({f.rule}){f.overridable ? "" : ` — ${t("cannot be overridden")}`}
          </li>
        ))}
      </ul>
      <p className="text-xs text-muted">{t("A missing rate or an inactive room type, hotel or service cannot be overridden.")}</p>
      {data.exception && (
        <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-line2 px-3 py-2">
          <span>
            {t("Exception until")} <DateText value={data.exception.expires_at} /> · {data.exception.reason}
          </span>
          <Button size="sm" variant="ghost" disabled={revoke.isPending} onClick={() => revoke.mutate()}>
            {t("Revoke")}
          </Button>
        </div>
      )}
      {rules.length > 0 ? (
        <fieldset className="flex flex-col gap-3">
          <legend className="mb-1 text-xs font-semibold text-muted">{t("Allow the sale despite")}</legend>
          {rules.map((r) => (
            <Checkbox
              key={r}
              label={t(RULE_LABEL[r])}
              checked={chosen.includes(r)}
              onChange={(e) => setChosen((c) => (e.target.checked ? [...c, r] : c.filter((x) => x !== r)))}
            />
          ))}
          <label className="flex flex-col gap-1">
            <span className="text-xs font-semibold text-muted">{t("Reason")}</span>
            <Textarea className="min-h-[90px] w-full" maxLength={500} value={reason} onChange={(e) => setReason(e.target.value)} />
            <span className="text-xs text-muted">{t("{0} / at least {1} characters", reason.trim().length, REASON_MIN)}</span>
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-xs font-semibold text-muted">{t("Days (at most {0})", data.exception_max_days)}</span>
            <Input size="sm" inputMode="numeric" className="w-28" value={days} onChange={(e) => setDays(e.target.value.replace(/\D/g, ""))} />
          </label>
          {allow.isError && <p className="text-xs text-danger">{errorText(allow.error)}</p>}
          <Button variant="primary" disabled={!ready || allow.isPending} onClick={() => allow.mutate()}>
            {t("Allow sale")}
          </Button>
          <p className="text-xs text-muted">{t("Trips sold under the exception get a trip check to book what is missing by hand.")}</p>
        </fieldset>
      ) : (
        <p className="text-muted">{t("Nothing on this tour can be overridden now.")}</p>
      )}
    </div>
  );
}
