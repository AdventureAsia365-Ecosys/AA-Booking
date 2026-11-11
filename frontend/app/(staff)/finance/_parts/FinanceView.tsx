"use client";
// app/(staff)/finance/_parts/FinanceView.tsx
// AA-1074 (bk-045) — Costs & profit: three tabs, each shown only with its permission.
//   profit      profit per trip case, departure and agency month (profit.read: admin, finance)
//   departures  departure shared costs, planned against actual (bill.read + pricing.read)
//   orders      supplier purchase orders (ops.read + pricing.read)
// Every figure is net cost: commercial data, never shown to agencies.

import { EmptyState, PageFrame, useCan, useUrlFilters } from "@/ui";
import { SavedTabs } from "../../_parts/SavedTabs";
import { DeparturesTab } from "./DeparturesTab";
import { OrdersTab } from "./OrdersTab";
import { ProfitTab } from "./ProfitTab";

export function FinanceView() {
  const { get } = useUrlFilters();
  const canProfit = useCan("profit.read");
  const canPricing = useCan("pricing.read");
  const canCosts = useCan("bill.read") && canPricing;
  const canOrders = useCan("ops.read") && canPricing;
  const tabs = [
    ...(canProfit ? [{ key: "profit", label: "Profit" }] : []),
    ...(canCosts ? [{ key: "departures", label: "Departure costs" }] : []),
    ...(canOrders ? [{ key: "orders", label: "Purchase orders" }] : []),
  ];
  const tab = tabs.some((t) => t.key === get("tab")) ? get("tab") : (tabs[0]?.key ?? "");
  return (
    <PageFrame
      title="Costs & profit"
      description="Supplier costs normalised to USD at the approved rates, shared costs of each departure, purchase orders to suppliers and the profit of every trip case, departure and agency."
    >
      {tabs.length === 0 ? (
        <EmptyState title="No access" description="Costs need the pricing reader permission; profit needs the finance or admin role." />
      ) : (
        <>
          <SavedTabs tabs={tabs} active={tab} ariaLabel="Costs and profit" />
          {tab === "profit" && <ProfitTab />}
          {tab === "departures" && <DeparturesTab />}
          {tab === "orders" && <OrdersTab />}
        </>
      )}
    </PageFrame>
  );
}
