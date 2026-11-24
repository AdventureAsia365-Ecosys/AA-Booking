"use client";
// app/(staff)/suppliers/_parts/ExpiringContracts.tsx — "Contracts ending soon" (AA-1151 B3, AA-1177 / Jira PR-31).
// The published supplier contracts of every kind (hotel, transport, guide, activity, ...) that end within the Pricing >
// Policy window contract_expiry_alert_days, soonest first: GET /booking/contracts/expiring (supply.read), the same list
// the daily reminder announces and e-mails. Shown on Suppliers (every supplier; the My work tile "Contracts ending soon"
// opens it with ?contracts=ending), on a supplier's Contracts tab (that supplier only) and on Add-ons. One tone: the
// only threshold is the policy window, no second cut-over in code (lesson S289).

import { useEffect, useRef } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { addonsApi, type ExpiringContract } from "@/lib/api/addons";
import { DateText, ErrorState, Pill, Section, useCan } from "@/ui";
import { useT } from "@/ui/core/useI18n";
import { CategoryChips } from "./shared";
import { MSG } from "./ExpiringContracts.messages";

export function ExpiringContracts({
  supplier,
  focus = false,
  showEmpty = false,
}: {
  /** One supplier's contracts (UUID or code); every supplier when left out. */
  supplier?: string;
  /** Scroll the list into view (the My work tile's link). */
  focus?: boolean;
  /** Say so when nothing ends soon (else the section is hidden). */
  showEmpty?: boolean;
}) {
  const t = useT(MSG);
  const canSupply = useCan("supply.read");
  const ref = useRef<HTMLDivElement>(null);
  const list = useQuery({
    queryKey: ["contracts-expiring", supplier ?? "all"],
    queryFn: () => addonsApi.expiring(supplier),
    enabled: canSupply,
  });
  const ready = !!list.data;
  useEffect(() => {
    if (focus && ready) ref.current?.scrollIntoView?.({ block: "start", behavior: "smooth" });
  }, [focus, ready]);

  if (!canSupply) return null;
  if (list.isError) {
    return <ErrorState message={t("The list of contracts ending soon could not load. {0}", (list.error as Error).message)} onRetry={() => list.refetch()} />;
  }
  if (!list.data || (list.data.items.length === 0 && !showEmpty)) return null;
  return (
    <div ref={ref} className="scroll-mt-4">
      <ExpiringSection days={list.data.window_days} items={list.data.items} supplier={supplier} />
    </div>
  );
}

function ExpiringSection({ days, items, supplier }: { days: number; items: ExpiringContract[]; supplier?: string }) {
  const t = useT(MSG);
  return (
    <Section
      testId="contracts-expiring"
      title={t("Contracts ending soon")}
      description={
        supplier
          ? t("This supplier's published contracts that end within {0} days (Pricing > Policy). Enter the next contract before then.", days)
          : t("Published supplier contracts of every kind that end within {0} days (Pricing > Policy). Enter the next contract before then.", days)
      }
    >
      {items.length === 0 ? (
        <p className="text-[13px] text-muted">{t("No published contract ends within {0} days.", days)}</p>
      ) : (
        <ul className="flex flex-col gap-2 text-[13px]" data-testid="contracts-expiring-list">
          {items.map((k) => (
            <ExpiringRow key={k.id} k={k} withSupplier={!supplier} />
          ))}
        </ul>
      )}
    </Section>
  );
}

function ExpiringRow({ k, withSupplier }: { k: ExpiringContract; withSupplier: boolean }) {
  const t = useT(MSG);
  return (
    <li className="flex min-w-0 flex-wrap items-center gap-2" data-testid="contracts-expiring-row">
      <Pill tone="warning">{k.days_left === 1 ? t("1 day left") : t("{0} days left", k.days_left)}</Pill>
      {withSupplier && (
        <Link href={`/suppliers/${encodeURIComponent(k.supplier_code)}?tab=contracts`} className="font-semibold">
          {k.supplier_name}
        </Link>
      )}
      {withSupplier && k.supplier_categories?.length ? <CategoryChips categories={k.supplier_categories} /> : null}
      <span className="min-w-0 truncate text-body" title={k.title}>
        {k.title}
      </span>
      <span className="text-muted">
        {t("ends")} <DateText value={k.valid_to} />
      </span>
      <ReminderState k={k} />
    </li>
  );
}

function ReminderState({ k }: { k: ExpiringContract }) {
  const t = useT(MSG);
  if (k.mailed) {
    return (
      <Pill tone="success" dot={false} title={t("The daily reminder e-mailed the staff roles set in Pricing > Policy.")}>
        {t("E-mailed")}
      </Pill>
    );
  }
  if (k.announced) {
    return (
      <Pill tone="neutral" dot={false} title={t("The daily reminder posted a notification; the e-mail is not sent yet.")}>
        {t("Notified")}
      </Pill>
    );
  }
  return (
    <Pill tone="neutral" dot={false} title={t("Not announced yet: the daily reminder runs at 01:00 UTC.")}>
      {t("Pending")}
    </Pill>
  );
}
