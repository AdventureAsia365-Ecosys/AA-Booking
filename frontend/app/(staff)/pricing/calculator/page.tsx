"use client";
// app/(staff)/pricing/calculator/page.tsx — Stand-alone quote preview (W1, UI spec §4.10, ADR 0005).
// Left: net cost lines (amount + supplier currency) and the channel / selling currency / peak / override
// settings. Right: the priced result. Every number comes from POST /booking/pricing/quote-preview
// (bk-004, AA-810), called live and debounced; nothing is saved and no case is involved.
// Route access is admin / sales / contracting (lib/rbac.ts), matching the API's pricing.read permission.

import { useState } from "react";
import { RotateCcw } from "lucide-react";
import { quoteRequest, newKey, type CostLine } from "@/lib/proposalDraft";
import { Button, PageFrame, Section, useStaff } from "@/ui";
import { CostLinesEditor, FxNotice, PriceSettings, QuoteResult, useQuotePreview, type PriceSettingsValue } from "@/ui/booking/PricePanel";

const DEFAULT_SETTINGS: PriceSettingsValue = { channel: "b2c_tailor_made", currency: "USD", peak: false, override: "", overrideReason: "" };

function blankLine(): CostLine {
  return { key: newKey("calc"), net_amount: "", net_currency: "USD", supplier_note: "" };
}

export default function PricingCalculatorPage() {
  const me = useStaff();
  const canManageRules = me.role === "admin" || me.role === "contracting";
  const [lines, setLines] = useState<CostLine[]>(() => [blankLine()]);
  const [settings, setSettings] = useState<PriceSettingsValue>(DEFAULT_SETTINGS);

  const request = quoteRequest(lines, settings);
  const quote = useQuotePreview(request);
  const fresh = quote.data && !quote.settling && !quote.isFetching && !quote.isError ? quote.data : null;
  const sellAmounts = lines.map((_, i) => (fresh && fresh.lines.length === lines.length ? fresh.lines[i].sell_amount : null));
  const incomplete = lines.length > 0 && !request;

  return (
    <PageFrame
      title="Pricing calculator"
      description="Price a set of net costs with the rule and FX rates in force today, without a trip case. Nothing is saved."
      actions={
        <Button
          variant="ghost"
          size="sm"
          onClick={() => {
            setLines([blankLine()]);
            setSettings(DEFAULT_SETTINGS);
          }}
        >
          <RotateCcw size={13} /> Reset
        </Button>
      }
    >
      <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <div className="flex flex-col gap-5">
          <Section title="Net costs" description="One line per supplier cost: the net amount and the supplier's currency.">
            <CostLinesEditor lines={lines} onChange={setLines} newLine={blankLine} sellAmounts={sellAmounts} sellCurrency={fresh?.currency} />
          </Section>
          <Section title="Settings" description="Channel, selling currency, peak season and an optional override total.">
            <div className="flex flex-col gap-3">
              <PriceSettings value={settings} onChange={setSettings} belowFloor={!!fresh?.below_floor} showReason={false} />
              <FxNotice />
            </div>
          </Section>
        </div>
        <Section title="Result" description="Rule in force, FX rates used, each line in USD and selling currency, rounding, margin and the floor check.">
          <QuoteResult
            query={quote}
            canManageRules={canManageRules}
            emptyHint={incomplete ? "Complete every cost line (amount and 3-letter currency) to see the price." : undefined}
          />
          {fresh?.below_floor && (
            <p className="mt-3 text-xs text-muted">
              The calculator only shows the check: a below-floor price needs a reason when it is applied to a proposal version.
            </p>
          )}
        </Section>
      </div>
    </PageFrame>
  );
}
