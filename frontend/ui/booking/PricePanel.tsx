"use client";
// ui/booking/PricePanel.tsx
// AA-683 — the price panel of the proposal builder and the stand-alone pricing calculator (UI spec §4.8,
// §4.10, §5 PricePanel). Three parts that pages compose:
//   - CostLinesEditor  net cost lines (amount + supplier currency + note);
//   - PriceSettings    channel, selling currency, peak toggle, optional override total (+ reason);
//   - QuoteResult      the answer of POST /booking/pricing/quote-preview: per-line sell amounts, subtotal,
//                      rounding, total, effective margin vs floor, the below-floor warning, rule and FX used.
// useQuotePreview() calls the API live, debounced 500 ms. Nothing here computes a price: every number on
// screen comes from the Booking pricing engine (bk-004), so the panel never disagrees with the API.

import { useEffect, useState } from "react";
import Link from "next/link";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { AlertTriangle, Calculator, ChevronDown, Plus, Trash2 } from "lucide-react";
import { ApiError } from "@/lib/api/client";
import { CHANNELS, SELLING_CURRENCIES, proposalsApi, type Channel, type QuotePreview, type QuotePreviewInput } from "@/lib/api/proposals";
import { costLineError, pct, type CostLine } from "@/lib/proposalDraft";
import { EmptyState, Spinner } from "../primitives";
import { formatMoney, Money } from "./Money";
import { DateText } from "./DateText";
import { useCan } from "../StaffContext";

const inputCls =
  "h-9 w-full min-w-0 rounded-md border border-line bg-card px-3 text-[13px] text-ink outline-none focus:border-accent-border disabled:bg-panel disabled:text-muted";

/** Supplier currencies seen in Asia tours; a suggestion list only, any ISO code is accepted. */
const COST_CURRENCIES = ["USD", "VND", "THB", "KHR", "LAK", "MMK", "IDR", "MYR", "SGD", "PHP", "JPY", "KRW", "CNY", "NPR", "BTN", "INR", "LKR", "MNT", "EUR", "GBP", "AUD"];

// ── Live quote ───────────────────────────────────────────────────────────────
export function useQuotePreview(request: QuotePreviewInput | null, delayMs = 500) {
  const key = request ? JSON.stringify(request) : null;
  const [debounced, setDebounced] = useState<string | null>(key);
  useEffect(() => {
    const t = window.setTimeout(() => setDebounced(key), delayMs);
    return () => window.clearTimeout(t);
  }, [key, delayMs]);
  const query = useQuery({
    queryKey: ["quote-preview", debounced],
    queryFn: () => proposalsApi.quotePreview(JSON.parse(debounced as string) as QuotePreviewInput),
    enabled: debounced !== null,
    retry: false,
    placeholderData: keepPreviousData,
    staleTime: 60_000,
  });
  return { ...query, settling: key !== debounced, hasRequest: key !== null };
}

// ── Cost lines ───────────────────────────────────────────────────────────────
export function CostLinesEditor({
  lines,
  onChange,
  newLine,
  disabled = false,
  sellAmounts,
  sellCurrency,
}: {
  lines: CostLine[];
  onChange: (next: CostLine[]) => void;
  newLine: () => CostLine;
  disabled?: boolean;
  /** Sell amount per line (same order), from the last quote. */
  sellAmounts?: (string | null)[];
  sellCurrency?: string;
}) {
  const update = (i: number, patch: Partial<CostLine>) => onChange(lines.map((l, j) => (j === i ? { ...l, ...patch } : l)));
  return (
    <div className="flex flex-col gap-2">
      <datalist id="bk-cost-currencies">
        {COST_CURRENCIES.map((c) => (
          <option key={c} value={c} />
        ))}
      </datalist>
      {lines.length === 0 && <p className="text-xs text-muted">No net cost yet.</p>}
      {lines.map((l, i) => {
        const err = l.net_amount || l.net_currency !== "USD" ? costLineError(l) : null;
        const sell = sellAmounts?.[i];
        return (
          <div key={l.key} className="rounded-md border border-line2 bg-panel p-2">
            <div className="grid grid-cols-[minmax(0,1fr)_84px_auto] items-center gap-2">
              <label className="sr-only" htmlFor={`${l.key}-amt`}>Net amount</label>
              <input
                id={`${l.key}-amt`}
                inputMode="decimal"
                placeholder="Net amount"
                value={l.net_amount}
                disabled={disabled}
                onChange={(e) => update(i, { net_amount: e.target.value.replace(/,/g, "") })}
                className={`${inputCls} bk-tabular`}
              />
              <label className="sr-only" htmlFor={`${l.key}-cur`}>Currency</label>
              <input
                id={`${l.key}-cur`}
                list="bk-cost-currencies"
                maxLength={3}
                value={l.net_currency}
                disabled={disabled}
                onChange={(e) => update(i, { net_currency: e.target.value.toUpperCase().replace(/[^A-Z]/g, "") })}
                className={`${inputCls} font-mono uppercase`}
              />
              <button
                type="button"
                aria-label="Remove cost line"
                disabled={disabled}
                onClick={() => onChange(lines.filter((_, j) => j !== i))}
                className="grid h-9 w-9 cursor-pointer place-items-center rounded-md text-muted hover:bg-danger-soft hover:text-danger disabled:cursor-not-allowed disabled:opacity-40"
              >
                <Trash2 size={15} />
              </button>
            </div>
            <div className="mt-2 grid grid-cols-[minmax(0,1fr)_auto] items-center gap-2">
              <label className="sr-only" htmlFor={`${l.key}-note`}>Supplier note</label>
              <input
                id={`${l.key}-note`}
                placeholder="Supplier / what this covers (optional)"
                maxLength={200}
                value={l.supplier_note}
                disabled={disabled}
                onChange={(e) => update(i, { supplier_note: e.target.value })}
                className={`${inputCls} h-8 text-xs`}
              />
              {sell != null && sellCurrency && (
                <span className="text-right text-xs text-muted">
                  sells <Money amount={sell} currency={sellCurrency} className="font-semibold text-ink" />
                </span>
              )}
            </div>
            {err && <p className="mt-1 text-xs text-danger">{err}</p>}
          </div>
        );
      })}
      {!disabled && (
        <button
          type="button"
          onClick={() => onChange([...lines, newLine()])}
          className="inline-flex cursor-pointer items-center gap-1.5 self-start rounded-md px-2 py-1 text-[12px] font-semibold text-accent-deep hover:bg-accent-tint"
        >
          <Plus size={14} /> Add net cost
        </button>
      )}
    </div>
  );
}

// ── Settings ─────────────────────────────────────────────────────────────────
export interface PriceSettingsValue {
  channel: Channel;
  currency: string;
  peak: boolean;
  override: string;
  overrideReason: string;
}

export function PriceSettings({
  value,
  onChange,
  belowFloor = false,
  disabled = false,
  showReason = true,
}: {
  value: PriceSettingsValue;
  onChange: (next: PriceSettingsValue) => void;
  belowFloor?: boolean;
  disabled?: boolean;
  /** The calculator has no reason field: nothing is applied there. */
  showReason?: boolean;
}) {
  const set = <K extends keyof PriceSettingsValue>(k: K, v: PriceSettingsValue[K]) => onChange({ ...value, [k]: v });
  const overrideBad = value.override.trim() !== "" && !/^\d{1,14}(\.\d{1,2})?$/.test(value.override.trim());
  return (
    <div className="grid grid-cols-2 gap-3">
      <label className="flex flex-col gap-1">
        <span className="text-[12px] font-medium text-ink">Channel</span>
        <select value={value.channel} disabled={disabled} onChange={(e) => set("channel", e.target.value as Channel)} className={inputCls}>
          {CHANNELS.map((c) => (
            <option key={c.value} value={c.value}>
              {c.label}
            </option>
          ))}
        </select>
      </label>
      <label className="flex flex-col gap-1">
        <span className="text-[12px] font-medium text-ink">Selling currency</span>
        <select value={value.currency} disabled={disabled} onChange={(e) => set("currency", e.target.value)} className={inputCls}>
          {SELLING_CURRENCIES.map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
        </select>
      </label>
      <label className="col-span-2 flex cursor-pointer items-center gap-2 text-[13px] text-ink">
        <input type="checkbox" checked={value.peak} disabled={disabled} onChange={(e) => set("peak", e.target.checked)} className="h-4 w-4 accent-accent" />
        Peak season dates (adds the rule&apos;s peak uplift)
      </label>
      <label className="col-span-2 flex flex-col gap-1">
        <span className="text-[12px] font-medium text-ink">
          Override total <span className="font-normal text-muted">(optional, in {value.currency})</span>
        </span>
        <input
          inputMode="decimal"
          placeholder="Leave empty to use the computed total"
          value={value.override}
          disabled={disabled}
          onChange={(e) => set("override", e.target.value.replace(/,/g, ""))}
          className={`${inputCls} bk-tabular`}
        />
        {overrideBad && <span className="text-xs text-danger">A positive amount, at most 2 decimals.</span>}
      </label>
      {showReason && belowFloor && value.override.trim() && (
        <label className="col-span-2 flex flex-col gap-1">
          <span className="text-[12px] font-medium text-danger">Reason for pricing below the floor *</span>
          <textarea
            rows={2}
            maxLength={500}
            value={value.overrideReason}
            disabled={disabled}
            onChange={(e) => set("overrideReason", e.target.value)}
            placeholder="e.g. repeat client, matched a competitor quote"
            className="rounded-md border border-danger-border bg-card px-3 py-2 text-[13px] text-ink outline-none focus:border-danger"
          />
        </label>
      )}
    </div>
  );
}

// ── FX notice (S236 review finding 5; AA-810 S237: rates are entered on /pricing/rules) ──────────────
/** Non-USD needs an approved, dated FX rate; admin / contracting get a link to the page that adds one. */
export function FxNotice() {
  const canManage = useCan("pricing.write");
  return (
    <p className="rounded-md bg-panel px-3 py-2 text-xs text-muted">
      Non-USD costs and selling currencies need a dated FX rate that a second person has approved.{" "}
      {canManage ? (
        <>
          Add or approve rates in{" "}
          <Link href="/pricing/rules?tab=fx" className="font-semibold text-accent-deep hover:underline">
            Pricing rules &amp; FX
          </Link>
          .
        </>
      ) : (
        "Ask an admin or contracting user to add one if a currency is missing."
      )}
    </p>
  );
}

// ── Result ───────────────────────────────────────────────────────────────────
function quoteErrorView(error: unknown, canManageRules: boolean) {
  const e = error instanceof ApiError ? error : null;
  if (e?.status === 404) {
    return {
      title: "The pricing engine is not on this Booking API yet",
      body: "Live quotes come from POST /booking/pricing/quote-preview, which ships with the pricing engine (bk-004, AA-810).",
      link: false,
    };
  }
  if (e?.status === 503) return { title: "Pricing data unavailable", body: "The Booking API could not read the pricing rules or FX rates. Try again in a moment.", link: false };
  if (e?.status === 403) return { title: "No access to pricing", body: "Your Booking role cannot see margins and net costs.", link: false };
  if (e?.status === 422 && (e.code.startsWith("no FX rate") || e.code.startsWith("no pricing rule"))) {
    // AA-810: rows are entered and approved (maker-checker) on /pricing/rules; only approved rows price.
    return {
      title: e.code.startsWith("no FX rate") ? `Missing FX rate: ${e.code.replace("no FX rate for ", "")}` : "No pricing rule in force",
      body: canManageRules
        ? "No approved row is in force for today. Add it in Pricing rules & FX and have a second person approve it, or price in USD."
        : "Price in USD for now, or ask an admin or contracting user to add and approve the missing row.",
      link: canManageRules,
    };
  }
  return { title: "Could not price this quote", body: (error as Error | null)?.message ?? "Unknown error.", link: false };
}

export function QuoteResult({
  query,
  canManageRules = false,
  emptyHint = "Add at least one net cost line to see the selling price.",
}: {
  query: ReturnType<typeof useQuotePreview>;
  canManageRules?: boolean;
  emptyHint?: string;
}) {
  const [details, setDetails] = useState(false);
  if (!query.hasRequest) {
    return <EmptyState icon={<Calculator size={26} />} title="No quote yet" description={emptyHint} />;
  }
  if (query.isError && !query.isFetching) {
    const v = quoteErrorView(query.error, canManageRules);
    return (
      <div role="alert" className="rounded-md border border-danger-border bg-danger-soft px-3 py-3 text-[13px] text-danger">
        <p className="font-semibold">{v.title}</p>
        <p className="mt-0.5 text-body">{v.body}</p>
        {v.link && (
          <Link href="/pricing/rules" className="mt-1 inline-block font-semibold text-accent-deep hover:underline">
            Open Pricing rules & FX
          </Link>
        )}
      </div>
    );
  }
  const q: QuotePreview | undefined = query.data;
  if (!q) {
    return (
      <div className="flex items-center gap-2 py-6 text-[13px] text-muted" aria-live="polite">
        <Spinner /> Pricing…
      </div>
    );
  }
  const busy = query.settling || query.isFetching;
  return (
    <div className={`flex flex-col gap-3 transition-opacity ${busy ? "opacity-60" : ""}`} aria-live="polite" aria-busy={busy}>
      <div className="flex items-baseline justify-between gap-3">
        <span className="text-[12px] font-medium uppercase tracking-[0.06em] text-muted">Total</span>
        <span className="font-display text-2xl font-semibold text-ink bk-tabular">{formatMoney(q.final_total, q.currency)}</span>
      </div>
      {q.below_floor && (
        <div role="alert" className="flex gap-2 rounded-md border border-danger-border bg-danger-soft px-3 py-2 text-[13px] text-danger">
          <AlertTriangle size={16} className="mt-0.5 shrink-0" />
          <span>
            Below floor ({pct(q.effective_margin)} margin, floor {pct(q.floor_margin)}): a reason is required.
          </span>
        </div>
      )}
      <dl className="grid grid-cols-[minmax(0,1fr)_auto] gap-x-3 gap-y-1.5 text-[13px]">
        <dt className="text-muted">Net cost (USD)</dt>
        <dd className="text-right"><Money amount={q.net_total_usd} currency="USD" /></dd>
        <dt className="text-muted">Subtotal</dt>
        <dd className="text-right"><Money amount={q.subtotal} currency={q.currency} /></dd>
        <dt className="text-muted">Computed total (rounded up to {q.rounding_increment})</dt>
        <dd className="text-right"><Money amount={q.total} currency={q.currency} /></dd>
        {q.override_total && (
          <>
            <dt className="text-muted">Override</dt>
            <dd className="text-right"><Money amount={q.override_total} currency={q.currency} strong /></dd>
          </>
        )}
        <dt className="text-muted">Margin applied</dt>
        <dd className="text-right text-ink">{pct(q.margin_applied)}</dd>
        {/* ADR 0005: the card fee is built into the price (never a surcharge); the rate comes from the rule. */}
        <dt className="text-muted">Card fee (included in the price)</dt>
        <dd className="text-right text-ink">{pct(q.rule.card_fee_pct)}</dd>
        <dt className="text-muted">Effective margin</dt>
        <dd className={`text-right font-semibold ${q.below_floor ? "text-danger" : "text-success"}`}>
          {pct(q.effective_margin)} <span className="font-normal text-muted">(floor {pct(q.floor_margin)})</span>
        </dd>
      </dl>
      <button
        type="button"
        onClick={() => setDetails((d) => !d)}
        aria-expanded={details}
        className="inline-flex cursor-pointer items-center gap-1 self-start text-[12px] font-semibold text-accent-deep"
      >
        <ChevronDown size={14} className={`transition-transform ${details ? "rotate-180" : ""}`} /> Rule, FX and lines
      </button>
      {details && (
        <div className="flex flex-col gap-3 rounded-md bg-panel p-3 text-xs text-body">
          <div>
            <p className="font-semibold text-ink">Rule in force ({CHANNELS.find((c) => c.value === q.rule.channel)?.label ?? q.rule.channel})</p>
            <p className="mt-0.5">
              Target {pct(q.rule.target_margin)} · floor {pct(q.rule.floor_margin)} · peak +{pct(q.rule.peak_uplift_pts)} · card fee {pct(q.rule.card_fee_pct)} · valid from <DateText value={q.rule.valid_from} />
            </p>
          </div>
          <div>
            <p className="font-semibold text-ink">FX rates used</p>
            {q.fx_rates.length === 0 ? (
              <p className="mt-0.5">None: every amount is in USD.</p>
            ) : (
              <ul className="mt-0.5 flex flex-col gap-0.5">
                {q.fx_rates.map((r) => (
                  <li key={r.currency} className="bk-tabular">
                    1 USD = {r.rate_per_usd} {r.currency} · buffer {pct(r.buffer_pct)} · as of <DateText value={r.as_of} /> · {r.source}
                  </li>
                ))}
              </ul>
            )}
          </div>
          <div>
            <p className="font-semibold text-ink">Lines</p>
            <ul className="mt-0.5 flex flex-col gap-0.5">
              {q.lines.map((l, i) => (
                <li key={i} className="grid grid-cols-[minmax(0,1fr)_auto] gap-2 bk-tabular">
                  <span>
                    {formatMoney(l.net_amount, l.net_currency)} → {formatMoney(l.net_usd, "USD")} net
                  </span>
                  <span className="text-ink">{formatMoney(l.sell_amount, q.currency)}</span>
                </li>
              ))}
            </ul>
          </div>
          <p className="text-muted">
            Priced on <DateText value={q.priced_on} />. Nothing is saved by this preview.
          </p>
        </div>
      )}
    </div>
  );
}
