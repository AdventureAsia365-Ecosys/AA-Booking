"use client";
// app/(staff)/suppliers/_parts/RateDrawer.tsx — Add / edit a net rate under a DRAFT supplier contract (AA-823, bk-010;
// ADR 0005 decisions 2 and 8). A rate prices one room type (per room night, optional meal plan) or one service (in the
// service's unit), in the contract currency, inside the contract validity, for a season label and a pax band. Writing
// a rate makes you the contract's maker: someone else publishes it, and then the rates are fixed.

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "@/lib/api/client";
import type { SupplierContract } from "@/lib/api/suppliers";
import { MEAL_PLANS, supplyApi, type MealPlan, type RateBody, type SupplierRate } from "@/lib/api/supply";
import { FormDrawer, useToast } from "@/ui";
import { FormField, errorFor, inputCls } from "@/app/(staff)/_parts/FormField";
import { UNIT_LABEL, rateTarget, supplyError } from "@/app/(staff)/_parts/supply";
import { textareaCls } from "@/app/(staff)/cases/_parts/fields";

const AMOUNT_RE = /^\d{1,14}(\.\d{1,2})?$/;
const MEAL_LABEL: Record<MealPlan, string> = { RO: "Room only", BB: "Bed & breakfast", HB: "Half board", FB: "Full board", AI: "All inclusive" };

export function RateDrawer({
  supplierId,
  contract,
  rate,
  onClose,
}: {
  supplierId: string;
  contract: SupplierContract;
  rate?: SupplierRate;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const toast = useToast();
  const options = useQuery({
    queryKey: ["supply", "options", supplierId],
    queryFn: () => supplyApi.options({ supplier_id: supplierId, limit: 50 }),
    enabled: !rate,
  });
  const initial = {
    target: rate ? (rate.hotel_room_type_id ? `room_type:${rate.hotel_room_type_id}` : `service:${rate.service_id}`) : "",
    season: rate?.season ?? "",
    valid_from: rate?.valid_from ?? contract.valid_from,
    valid_to: rate?.valid_to ?? contract.valid_to ?? "",
    meal_plan: rate?.meal_plan ?? "",
    pax_min: rate?.pax_min != null ? String(rate.pax_min) : "",
    pax_max: rate?.pax_max != null ? String(rate.pax_max) : "",
    net_amount: rate?.net_amount ?? "",
    notes: rate?.notes ?? "",
  };
  const [form, setForm] = useState(initial);
  const [localError, setLocalError] = useState<string | null>(null);
  const set = (k: keyof typeof form) => (v: string) => setForm((f) => ({ ...f, [k]: v }));
  const isRoom = form.target.startsWith("room_type:");
  const picked = options.data?.items.find((o) => `${o.kind}:${o.id}` === form.target);
  const unit = rate ? rate.unit : picked?.unit;

  const save = useMutation({
    mutationFn: (body: RateBody) => (rate ? supplyApi.updateRate(rate.id, body) : supplyApi.createRate(contract.id, body)),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["rates"] });
      qc.invalidateQueries({ queryKey: ["supplier"] });
      qc.invalidateQueries({ queryKey: ["hotel"] });
      qc.invalidateQueries({ queryKey: ["service"] });
      toast.success(rate ? "Rate saved." : "Rate added to the draft. Someone else publishes the contract.");
      onClose();
    },
  });

  function submit() {
    if (!rate && !form.target) return setLocalError("Pick the room type or service this rate prices.");
    const amount = form.net_amount.trim().replace(/,/g, "");
    if (!AMOUNT_RE.test(amount)) return setLocalError("Net amount is a number with at most 2 decimals, e.g. 1200000 or 45.50.");
    if (!form.valid_from) return setLocalError("Pick the first day the rate applies.");
    if (form.valid_to && form.valid_to < form.valid_from) return setLocalError("“Valid to” is on or after “valid from”.");
    const lo = form.pax_min ? Number(form.pax_min) : null;
    const hi = form.pax_max ? Number(form.pax_max) : null;
    if ((lo !== null && !(lo >= 1)) || (hi !== null && !(hi >= 1))) return setLocalError("Pax numbers start at 1.");
    if (lo !== null && hi !== null && hi < lo) return setLocalError("The pax band ends at or after where it starts.");
    setLocalError(null);
    const body: RateBody = {
      season: form.season.trim() || null,
      valid_from: form.valid_from,
      valid_to: form.valid_to || null,
      meal_plan: isRoom || rate?.hotel_room_type_id ? ((form.meal_plan || null) as MealPlan | null) : null,
      pax_min: lo,
      pax_max: hi,
      net_amount: amount,
      notes: form.notes.trim() || null,
    };
    if (!rate) {
      const [kind, id] = form.target.split(":");
      if (kind === "room_type") body.hotel_room_type_id = id;
      else body.service_id = id;
    }
    save.mutate(body);
  }

  const fe = save.error instanceof ApiError ? save.error.fieldErrors : [];
  const roomRate = rate ? !!rate.hotel_room_type_id : isRoom;
  return (
    <FormDrawer
      open
      onClose={onClose}
      title={rate ? "Edit rate" : "Add rate"}
      submitLabel={rate ? "Save rate" : "Add rate"}
      onSubmit={submit}
      busy={save.isPending}
      dirty={JSON.stringify(form) !== JSON.stringify(initial)}
      error={localError ?? (fe.length ? null : supplyError(save.error))}
      fieldErrors={fe}
      width={520}
    >
      <p className="text-[13px] text-muted">
        Under <strong className="text-ink">{contract.title}</strong> in <strong className="text-ink">{contract.currency}</strong>. Saving makes you this
        draft&apos;s maker: another person publishes it, and then its rates are fixed.
      </p>
      {rate ? (
        <FormField label="Prices" locked hint="A rate stays on what it prices; add a new rate for something else.">
          <input className={inputCls} value={rateTarget(rate)} disabled />
        </FormField>
      ) : (
        <FormField label="Prices" required hint="Active room types and services of this supplier." error={errorFor(fe, "hotel_room_type_id")}>
          <select className={inputCls} value={form.target} onChange={(e) => set("target")(e.target.value)} disabled={options.isLoading}>
            <option value="">
              {options.isLoading ? "Loading…" : options.data?.items.length ? "Pick a room type or service" : "This supplier has no active hotel room or service yet"}
            </option>
            {(options.data?.items ?? []).map((o) => (
              <option key={`${o.kind}:${o.id}`} value={`${o.kind}:${o.id}`}>
                {o.kind === "room_type" ? "Room · " : "Service · "}
                {o.label} ({o.code})
              </option>
            ))}
          </select>
        </FormField>
      )}
      {unit && <p className="-mt-2 text-xs text-muted">Net is {UNIT_LABEL[unit]}.</p>}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <FormField label="Net amount" required hint={`In ${contract.currency}.`} error={errorFor(fe, "net_amount")}>
          <input className={`${inputCls} bk-tabular`} inputMode="decimal" value={form.net_amount} onChange={(e) => set("net_amount")(e.target.value)} placeholder="1200000" />
        </FormField>
        <FormField label="Season" hint="A label, e.g. Low, Peak, Christmas." error={errorFor(fe, "season")}>
          <input className={inputCls} maxLength={80} value={form.season} onChange={(e) => set("season")(e.target.value)} />
        </FormField>
        <FormField label="Valid from" required error={errorFor(fe, "valid_from")}>
          <input type="date" className={inputCls} value={form.valid_from} min={contract.valid_from} max={contract.valid_to ?? undefined} onChange={(e) => set("valid_from")(e.target.value)} />
        </FormField>
        <FormField label="Valid to" hint={contract.valid_to ? "Inside the contract." : "Empty = open-ended."} error={errorFor(fe, "valid_to")}>
          <input type="date" className={inputCls} value={form.valid_to} min={form.valid_from} max={contract.valid_to ?? undefined} onChange={(e) => set("valid_to")(e.target.value)} />
        </FormField>
        <FormField label="Pax from" hint="Empty = any." error={errorFor(fe, "pax_min")}>
          <input type="number" min={1} max={999} className={inputCls} value={form.pax_min} onChange={(e) => set("pax_min")(e.target.value)} />
        </FormField>
        <FormField label="Pax to" error={errorFor(fe, "pax_max")}>
          <input type="number" min={1} max={999} className={inputCls} value={form.pax_max} onChange={(e) => set("pax_max")(e.target.value)} />
        </FormField>
      </div>
      {roomRate && (
        <FormField label="Meal plan" error={errorFor(fe, "meal_plan")}>
          <select className={inputCls} value={form.meal_plan} onChange={(e) => set("meal_plan")(e.target.value)}>
            <option value="">Not stated</option>
            {MEAL_PLANS.map((m) => (
              <option key={m} value={m}>
                {m} · {MEAL_LABEL[m]}
              </option>
            ))}
          </select>
        </FormField>
      )}
      <FormField label="Notes" hint="Supplements, child policy… Staff only." error={errorFor(fe, "notes")}>
        <textarea className={textareaCls} maxLength={2000} value={form.notes} onChange={(e) => set("notes")(e.target.value)} />
      </FormField>
    </FormDrawer>
  );
}
