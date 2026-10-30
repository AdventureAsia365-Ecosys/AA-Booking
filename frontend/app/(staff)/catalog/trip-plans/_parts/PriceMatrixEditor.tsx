"use client";
// app/(staff)/catalog/trip-plans/_parts/PriceMatrixEditor.tsx — the price matrix of a trip plan in the commercial
// drawer (AA-996 Figma audit item 1; Figma CREATE / EDIT TEMPLATE "Pricing" card; ui-spec §3 /commercial "price
// matrix"; root ADR 0005 d3/d6; bk-032). One card per row: channel, season label and validity, pax band, selling
// currency and the price per person. A row may instead carry the net cost per person: the API prices it on save with
// the channel's rule, the approved FX rate and the rounding of quote-preview (a price typed next to a net cost is a
// sales override). Rows of one channel and currency may not overlap (checked here and in the API).

import { Plus, Trash2 } from "lucide-react";
import { CHANNELS, COST_CURRENCIES, SELLING_CURRENCIES, type Channel } from "@/lib/api/proposals";
import type { PriceRow } from "@/lib/api/tripPlans";
import { Checkbox, Input, Select, DateField, Money, formatDay } from "@/ui";
import { marginLabel, newDraft, paxBandLabel, type DraftError, type PriceDraft } from "@/ui/booking/tripPlans";
import { inputCls } from "../../../_parts/FormField";

const CHANNEL_SHORT: Record<string, string> = Object.fromEntries(CHANNELS.map((c) => [c.value, c.label]));

function Field({ label, error, children, className = "" }: { label: string; error?: string; children: React.ReactNode; className?: string }) {
  return (
    <label className={`flex min-w-0 flex-col gap-1 ${className}`}>
      <span className="text-[12px] font-medium text-ink">{label}</span>
      {children}
      {error && <span className="text-[11.5px] text-danger">{error}</span>}
    </label>
  );
}

export function PriceMatrixEditor({
  rows,
  onChange,
  errors,
  saved,
  today,
}: {
  rows: PriceDraft[];
  onChange: (rows: PriceDraft[]) => void;
  errors: DraftError[];
  /** The rows as last saved (a draft from a saved row has the key `row-<position>`), for the "Saved:" line. */
  saved: PriceRow[];
  today: string;
}) {
  const set = (i: number, patch: Partial<PriceDraft>) => onChange(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  const errorOf = (i: number, field: DraftError["field"]) => errors.find((e) => e.row === i && e.field === field)?.message;

  return (
    <fieldset className="flex flex-col gap-3" aria-label="Price matrix">
      <legend className="mb-1 flex w-full items-baseline justify-between gap-3">
        <span className="text-[13px] font-medium text-ink">Price matrix</span>
        <span className="text-xs text-muted">Per person</span>
      </legend>
      <p className="-mt-1 text-xs text-muted">
        One row per channel, season and pax band. Enter the price per person, or the net cost per person to price it with the channel&apos;s pricing rule and
        today&apos;s approved FX rate (a price next to a net cost overrides it).
      </p>
      {rows.length === 0 && (
        <p className="rounded-md border border-dashed border-line px-3 py-4 text-center text-[13px] text-muted">No prices yet. Add the first row.</p>
      )}
      {rows.map((r, i) => {
        const last = saved.find((x) => `row-${x.position}` === r.key);
        const rowError = errorOf(i, "row");
        const band = Number(r.pax_min) >= 1 ? paxBandLabel(Number(r.pax_min), r.pax_max.trim() === "" ? null : Number(r.pax_max)) : "";
        return (
          <div key={r.key} className={`rounded-md border bg-card p-3 ${rowError ? "border-danger-border" : "border-line"}`} data-testid="price-row">
            <div className="mb-2 flex items-center justify-between gap-2">
              <span className="min-w-0 truncate text-[12.5px] font-semibold text-ink">
                Row {i + 1}
                <span className="font-normal text-muted">
                  {" "}
                  · {CHANNEL_SHORT[r.channel]}
                  {band && ` · ${band}`}
                </span>
              </span>
              <button
                type="button"
                className="grid h-7 w-7 shrink-0 place-items-center rounded-md text-muted hover:bg-danger-soft hover:text-danger"
                aria-label={`Remove price row ${i + 1}`}
                onClick={() => onChange(rows.filter((_, j) => j !== i))}
              >
                <Trash2 size={14} />
              </button>
            </div>
            <div className="grid grid-cols-2 gap-x-3 gap-y-2.5 sm:grid-cols-4">
              <Field label="Channel" className="col-span-2">
                <Select className={inputCls} value={r.channel} onChange={(e) => set(i, { channel: e.target.value as Channel })}>
                  {CHANNELS.map((c) => (
                    <option key={c.value} value={c.value}>
                      {c.label}
                    </option>
                  ))}
                </Select>
              </Field>
              <div className="col-span-2 flex min-w-0 items-start gap-3">
                <Field label="Season" error={errorOf(i, "season")} className="flex-1">
                  <Input className={inputCls} maxLength={80} placeholder="Low, High, Peak…" value={r.season} onChange={(e) => set(i, { season: e.target.value })} />
                </Field>
                {/* Its own label (not inside the Season label, which names only its first control). */}
                <label className="mt-[22px] flex h-10 shrink-0 cursor-pointer items-center gap-1.5 text-[12.5px] text-body">
                  <Checkbox checked={r.peak} onChange={(e) => set(i, { peak: e.target.checked })} />
                  Peak
                </label>
              </div>
              <Field label="Valid from" error={errorOf(i, "valid_from")} className="col-span-2 sm:col-span-2">
                <DateField className={inputCls} value={r.valid_from} onChange={(v) => set(i, { valid_from: v })} aria-label={`Row ${i + 1} valid from`} />
              </Field>
              <Field label="Valid to" error={errorOf(i, "valid_to")} className="col-span-2 sm:col-span-2">
                <DateField className={inputCls} value={r.valid_to} min={r.valid_from || undefined} onChange={(v) => set(i, { valid_to: v })} aria-label={`Row ${i + 1} valid to`} />
              </Field>
              <Field label="Pax from" error={errorOf(i, "pax_min")}>
                <Input className={inputCls} inputMode="numeric" value={r.pax_min} onChange={(e) => set(i, { pax_min: e.target.value })} />
              </Field>
              <Field label="Pax to" error={errorOf(i, "pax_max")}>
                <Input className={inputCls} inputMode="numeric" placeholder="and more" value={r.pax_max} onChange={(e) => set(i, { pax_max: e.target.value })} />
              </Field>
              <Field label="Currency">
                <Select className={inputCls} value={r.currency} onChange={(e) => set(i, { currency: e.target.value })}>
                  {SELLING_CURRENCIES.map((c) => (
                    <option key={c}>{c}</option>
                  ))}
                </Select>
              </Field>
              <Field label="Price / person" error={errorOf(i, "amount")}>
                <Input
                  className={inputCls}
                  inputMode="decimal"
                  placeholder={r.net_amount.trim() ? "computed" : "amount"}
                  value={r.amount}
                  onChange={(e) => set(i, { amount: e.target.value })}
                />
              </Field>
              <Field label="Net cost / person (optional)" error={errorOf(i, "net_amount")} className="col-span-2 sm:col-span-4">
                <span className="grid grid-cols-[minmax(0,1fr)_96px] gap-3">
                  <Input className={inputCls} inputMode="decimal" placeholder="net cost to price from" value={r.net_amount} onChange={(e) => set(i, { net_amount: e.target.value })} />
                  <Select aria-label={`Row ${i + 1} net cost currency`} className={inputCls} value={r.net_currency} onChange={(e) => set(i, { net_currency: e.target.value })}>
                    {COST_CURRENCIES.map((c) => (
                      <option key={c}>{c}</option>
                    ))}
                  </Select>
                </span>
              </Field>
            </div>
            {last?.priced && (
              <p className="mt-2 text-xs text-muted">
                Saved: <Money amount={last.amount} currency={last.currency} /> {last.overridden ? "(override)" : "from the net cost"}
                {marginLabel(last.effective_margin) && ` · margin ${marginLabel(last.effective_margin)}`}
                {last.priced_on && ` · priced ${formatDay(last.priced_on)}`}
              </p>
            )}
            {rowError && <p className="mt-2 text-xs text-danger">{rowError}</p>}
          </div>
        );
      })}
      <button
        type="button"
        className="inline-flex items-center gap-1.5 self-start rounded-md px-1 text-[13px] font-medium text-accent-deep hover:underline"
        onClick={() => onChange([...rows, newDraft(rows[rows.length - 1], today)])}
      >
        <Plus size={14} /> Add price row
      </button>
    </fieldset>
  );
}
