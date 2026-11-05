"use client";
// app/(staff)/pricing/agency-tiers/_parts/AgencyTiersView.tsx — Pricing → Agency tiers (AA-1030, bk-043; root ADR 0019
// b15). Three tabs: Tiers (the discount each tier takes off the agency price today, the margin it leaves under the
// b2b rule with a below-floor warning, and the dated discount rows with maker-checker), Agencies (every B2B partner
// with its tier, moved with one select; audited) and Rate cards (a partner's contract discount, for every tour or
// one tour, with maker-checker; an approved card is ended by an end card, from its "End" button). AA-1030 G3: each agency
// row links to its tariff (../tariff, printable, CSV). Reads need pricing.read; writes and approvals pricing.write, and the API refuses an
// approval by the row's creator (403 own_row): the Approve button follows each row's `can_approve`.

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { Plus } from "lucide-react";
import { ApiError } from "@/lib/api/client";
import {
  agencyPricingApi,
  type AgencyRowStatus,
  type PartnerTerms,
  type PartnerTier,
  type RateCardRow,
  type TierDiscountRow,
} from "@/lib/api/agencyPricing";
import { pct } from "@/lib/proposalDraft";
import {
  Button,
  ConfirmDialog,
  DataTable,
  DateText,
  PageFrame,
  Pill,
  Section,
  Select,
  useCan,
  useToast,
  useUrlFilters,
  type BookingTone,
  type ColumnDef,
} from "@/ui";
import { SavedTabs } from "../../../_parts/SavedTabs";
import { NewRateCardDrawer, NewTierDiscountDrawer, NewTierDrawer, type RateCardKind } from "./AgencyDrawers";

const STATUS_META: Record<AgencyRowStatus, { label: string; tone: BookingTone; help: string }> = {
  pending: { label: "Pending", tone: "warning", help: "Waiting for a second person. Not used for prices." },
  in_force: { label: "In force", tone: "success", help: "Used for quotes today." },
  scheduled: { label: "Scheduled", tone: "info", help: "Approved; takes over on its date." },
  superseded: { label: "Superseded", tone: "neutral", help: "Approved, replaced by a newer approved row." },
  expired: { label: "Expired", tone: "neutral", help: "Approved; its last day is over." },
};

function StatusPill({ status }: { status: AgencyRowStatus }) {
  const meta = STATUS_META[status] ?? { label: status, tone: "neutral" as const, help: "" };
  return (
    <Pill tone={meta.tone} title={meta.help}>
      {meta.label}
    </Pill>
  );
}

function approveErrorText(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.code === "own_row") return "You created this row. A second person has to approve it (maker-checker).";
    if (err.code === "already_approved") return "Someone approved this row already. The list is refreshed.";
  }
  return err instanceof Error ? err.message : "Could not approve the row.";
}

type Approving = { kind: "discount"; row: TierDiscountRow } | { kind: "card"; row: RateCardRow } | null;
type DrawerKind = "tier" | "discount" | "card" | null;
type CardPrefill = { partner: string; tour?: string; kind: RateCardKind } | null;

/** The discount of a rate card row, or what an end card does. */
function cardTerms(row: RateCardRow): string {
  return row.discount_pct === null ? "Ends contract rate" : pct(row.discount_pct);
}

export function AgencyTiersView() {
  const canWrite = useCan("pricing.write");
  const qc = useQueryClient();
  const toast = useToast();
  const { get } = useUrlFilters();
  const tab = get("tab") === "agencies" ? "agencies" : get("tab") === "cards" ? "cards" : "tiers";
  const [drawer, setDrawer] = useState<DrawerKind>(null);
  const [approving, setApproving] = useState<Approving>(null);
  const [cardPrefill, setCardPrefill] = useState<CardPrefill>(null);

  const tiers = useQuery({ queryKey: ["agency-pricing", "tiers"], queryFn: () => agencyPricingApi.tiers() });
  const discounts = useQuery({ queryKey: ["agency-pricing", "discounts"], queryFn: () => agencyPricingApi.discounts() });
  const partners = useQuery({ queryKey: ["agency-pricing", "terms"], queryFn: () => agencyPricingApi.partnerTerms() });
  const cards = useQuery({ queryKey: ["agency-pricing", "cards"], queryFn: () => agencyPricingApi.rateCards() });
  const tierRows = useMemo(() => tiers.data?.items ?? [], [tiers.data]);
  const discountRows = useMemo(() => discounts.data?.items ?? [], [discounts.data]);
  const partnerRows = useMemo(() => partners.data?.items ?? [], [partners.data]);
  const cardRows = useMemo(() => cards.data?.items ?? [], [cards.data]);
  const rule = tiers.data?.b2b_rule ?? null;

  const approve = useMutation({
    mutationFn: (a: NonNullable<Approving>): Promise<TierDiscountRow | RateCardRow> =>
      a.kind === "discount" ? agencyPricingApi.approveDiscount(a.row.id) : agencyPricingApi.approveRateCard(a.row.id),
    onSuccess: (row, a) => {
      qc.invalidateQueries({ queryKey: ["agency-pricing"] });
      toast.success(`${a.kind === "discount" ? "Discount" : "Rate card"} approved: ${STATUS_META[row.status]?.label ?? row.status}.`);
      setApproving(null);
    },
    onError: (err) => {
      if (err instanceof ApiError && err.code === "already_approved") qc.invalidateQueries({ queryKey: ["agency-pricing"] });
    },
  });

  const moveTier = useMutation({
    mutationFn: ({ partner, tier }: { partner: PartnerTerms; tier: string }) => agencyPricingApi.setPartnerTier(partner.id, tier),
    onSuccess: (row) => {
      qc.invalidateQueries({ queryKey: ["agency-pricing"] });
      toast.success(`${row.agency_name} is now ${row.tier_label} (${pct(row.effective_discount)} off the agency price).`);
    },
    onError: (err) => toast.error(err instanceof Error ? err.message : "Could not change the tier."),
  });

  function statusCell(status: AgencyRowStatus, canApprove: boolean, onApprove: () => void) {
    if (status !== "pending") return <StatusPill status={status} />;
    return (
      <span className="flex flex-col items-end gap-1.5 md:items-start">
        <StatusPill status={status} />
        {canApprove ? (
          <span onClick={(e) => e.stopPropagation()}>
            <Button size="sm" variant="secondary" onClick={onApprove} title="Approve this row (maker-checker)">
              Approve
            </Button>
          </span>
        ) : (
          <span className="block text-xs leading-snug text-muted md:max-w-[96px]">{canWrite ? "Needs a second person" : "Waiting"}</span>
        )}
      </span>
    );
  }

  const tierColumns: ColumnDef<PartnerTier, unknown>[] = [
    {
      id: "tier",
      header: "Tier",
      accessorKey: "label",
      cell: ({ row }) => (
        <span className="flex flex-col">
          <span className="font-medium text-ink">{row.original.label}</span>
          <span className="font-mono text-xs text-muted">{row.original.code}</span>
        </span>
      ),
    },
    { id: "discount", header: "Discount today", accessorFn: (r) => Number(r.discount_in_force), cell: ({ row }) => pct(row.original.discount_in_force) },
    {
      id: "margin",
      header: "AA margin left",
      accessorFn: (r) => Number(r.margin_after_discount ?? -1),
      cell: ({ row }) =>
        row.original.margin_after_discount === null ? (
          <span className="text-muted">—</span>
        ) : (
          <span className="flex flex-col items-end gap-1 md:items-start">
            <span className="tabular-nums">{pct(row.original.margin_after_discount)}</span>
            {row.original.below_floor && (
              <Pill tone="danger" title="The margin left is under the b2b rule's floor. Quotes still use the discount.">
                Below floor
              </Pill>
            )}
          </span>
        ),
    },
    { id: "partners", header: "Agencies", accessorKey: "partners" },
    {
      id: "pending",
      header: "Pending rows",
      accessorKey: "pending",
      cell: ({ row }) => (row.original.pending ? <Pill tone="warning">{row.original.pending}</Pill> : <span className="text-muted">—</span>),
    },
  ];

  const discountColumns: ColumnDef<TierDiscountRow, unknown>[] = [
    { id: "tier", header: "Tier", accessorKey: "tier_label", cell: ({ row }) => <span className="font-medium text-ink">{row.original.tier_label}</span> },
    { id: "discount", header: "Discount", accessorFn: (r) => Number(r.discount_pct), cell: ({ row }) => pct(row.original.discount_pct) },
    { id: "valid_from", header: "Valid from", accessorKey: "valid_from", cell: ({ row }) => <DateText value={row.original.valid_from} /> },
    {
      id: "status",
      header: "Status",
      accessorKey: "status",
      cell: ({ row }) =>
        statusCell(row.original.status, row.original.can_approve, () => {
          approve.reset();
          setApproving({ kind: "discount", row: row.original });
        }),
    },
    { id: "entered", header: "Entered by", accessorFn: (r) => r.created_by?.name ?? r.created_by?.id ?? "", cell: ({ row }) => personText(row.original.created_by) },
    { id: "approved", header: "Approved by", accessorFn: (r) => r.approved_by?.name ?? r.approved_by?.id ?? "", cell: ({ row }) => personText(row.original.approved_by) },
    { id: "note", header: "Note", accessorFn: (r) => r.note ?? "", cell: ({ row }) => <span className="text-[13px] text-muted">{row.original.note ?? "—"}</span> },
  ];

  const partnerColumns: ColumnDef<PartnerTerms, unknown>[] = [
    {
      id: "agency",
      header: "Agency",
      accessorKey: "agency_name",
      cell: ({ row }) => (
        <span className="flex flex-col">
          <span className="font-medium text-ink">{row.original.agency_name}</span>
          {row.original.status === "suspended" && <span className="text-xs text-muted">Suspended</span>}
        </span>
      ),
    },
    {
      id: "tier",
      header: "Tier",
      accessorKey: "tier_label",
      cell: ({ row }) =>
        canWrite ? (
          <span onClick={(e) => e.stopPropagation()}>
            <Select
              size="sm"
              aria-label={`Tier of ${row.original.agency_name}`}
              value={row.original.tier_code}
              disabled={moveTier.isPending}
              onChange={(e) => moveTier.mutate({ partner: row.original, tier: e.target.value })}
            >
              {tierRows.map((t) => (
                <option key={t.code} value={t.code}>
                  {t.label}
                </option>
              ))}
            </Select>
          </span>
        ) : (
          row.original.tier_label
        ),
    },
    { id: "tier_discount", header: "Tier discount", accessorFn: (r) => Number(r.tier_discount), cell: ({ row }) => pct(row.original.tier_discount) },
    {
      id: "card",
      header: "Rate card",
      accessorFn: (r) => Number(r.card_discount ?? -1),
      cell: ({ row }) => (
        <span className="flex flex-col">
          <span>{row.original.card_discount ? `${pct(row.original.card_discount)} every tour` : "—"}</span>
          {row.original.plan_cards > 0 && (
            <span className="text-xs text-muted">
              + {row.original.plan_cards} tour card{row.original.plan_cards > 1 ? "s" : ""}
            </span>
          )}
        </span>
      ),
    },
    {
      id: "effective",
      header: "Off the agency price",
      accessorFn: (r) => Number(r.effective_discount),
      cell: ({ row }) => <strong className="tabular-nums text-ink">{pct(row.original.effective_discount)}</strong>,
    },
    {
      // AA-1030 G3: the agency's own price per tour, season and pax band (printable, CSV).
      id: "tariff",
      header: "Tariff",
      enableSorting: false,
      cell: ({ row }) => (
        <Link
          href={`/pricing/agency-tiers/tariff?partner=${encodeURIComponent(row.original.id)}`}
          onClick={(e) => e.stopPropagation()}
          className="whitespace-nowrap text-[13px] font-semibold text-accent-deep hover:underline"
        >
          View tariff
        </Link>
      ),
    },
  ];

  const cardColumns: ColumnDef<RateCardRow, unknown>[] = [
    { id: "agency", header: "Agency", accessorKey: "agency_name", cell: ({ row }) => <span className="font-medium text-ink">{row.original.agency_name}</span> },
    {
      id: "scope",
      header: "Tours",
      accessorFn: (r) => r.source_id ?? "",
      cell: ({ row }) =>
        row.original.source_id ? (
          <span className="block max-w-[160px] truncate font-mono text-xs" title={row.original.source_id}>
            {row.original.source_id}
          </span>
        ) : (
          "Every tour"
        ),
    },
    {
      id: "discount",
      header: "Discount",
      accessorFn: (r) => (r.discount_pct === null ? -1 : Number(r.discount_pct)),
      cell: ({ row }) =>
        row.original.discount_pct === null ? <span className="text-[13px] text-muted">{cardTerms(row.original)}</span> : cardTerms(row.original),
    },
    {
      id: "validity",
      header: "Valid",
      accessorKey: "valid_from",
      cell: ({ row }) => (
        <span className="flex flex-col whitespace-nowrap text-[13px]">
          <DateText value={row.original.valid_from} />
          <span className="text-xs text-muted">{row.original.valid_to ? <>to <DateText value={row.original.valid_to} /></> : "no end date"}</span>
        </span>
      ),
    },
    {
      id: "status",
      header: "Status",
      accessorKey: "status",
      cell: ({ row }) =>
        statusCell(row.original.status, row.original.can_approve, () => {
          approve.reset();
          setApproving({ kind: "card", row: row.original });
        }),
    },
    { id: "ref", header: "Contract", accessorFn: (r) => r.contract_ref ?? "", cell: ({ row }) => <span className="text-[13px] text-muted">{row.original.contract_ref ?? "—"}</span> },
    { id: "entered", header: "Entered by", accessorFn: (r) => r.created_by?.name ?? "", cell: ({ row }) => personText(row.original.created_by) },
    {
      id: "end",
      header: "",
      enableSorting: false,
      cell: ({ row }) =>
        canWrite && row.original.discount_pct !== null && (row.original.status === "in_force" || row.original.status === "scheduled") ? (
          <Button
            variant="ghost"
            size="sm"
            title="Add an end card for this agency and tours (back to the tier); a second person approves it"
            onClick={() =>
              setCardPrefill({ partner: row.original.partner_id, tour: row.original.source_id ?? undefined, kind: "end" })
            }
          >
            End
          </Button>
        ) : null,
    },
  ];

  const pendingCount = discountRows.filter((r) => r.status === "pending").length;
  const pendingCards = cardRows.filter((r) => r.status === "pending").length;

  return (
    <PageFrame
      title="Agency tiers"
      description="Each agency's tier takes its discount off the agency price of every B2B quote; a contract rate card replaces the tier for one agency. The suggested retail price stays the same for every agency."
      actions={
        canWrite ? (
          <div className="flex flex-wrap gap-2">
            {tab === "tiers" && (
              <>
                <Button variant="secondary" size="sm" onClick={() => setDrawer("tier")}>
                  <Plus size={13} /> New tier
                </Button>
                <Button variant="primary" size="sm" onClick={() => setDrawer("discount")} disabled={!tierRows.length}>
                  <Plus size={13} /> New discount
                </Button>
              </>
            )}
            {tab !== "tiers" && (
              <Button variant="primary" size="sm" onClick={() => setDrawer("card")} disabled={!partnerRows.length}>
                <Plus size={13} /> New rate card
              </Button>
            )}
          </div>
        ) : undefined
      }
    >
      <div className="rounded-lg border border-accent-border bg-accent-tint px-4 py-3 text-[13px] text-body">
        <strong className="text-ink">Maker-checker.</strong> A new discount or rate card starts as <em>Pending</em> and
        is not used until another staff member approves it. Rows are never edited: enter a new row with a later date.
        {rule && (
          <>
            {" "}
            B2B rule in force: target margin {pct(rule.target_margin)}, floor {pct(rule.floor_margin)}, card fee{" "}
            {pct(rule.card_fee_pct)}
            {rule.rrp_markup_pct ? `, suggested retail price = agency price + ${pct(rule.rrp_markup_pct)}` : ""}.
          </>
        )}
      </div>
      <SavedTabs
        tabs={[
          { key: "tiers", label: "Tiers", count: pendingCount || undefined },
          { key: "agencies", label: "Agencies" },
          { key: "cards", label: "Rate cards", count: pendingCards || undefined },
        ]}
      />
      {tab === "tiers" && (
        <>
          <Section title="Tiers" description="The discount in force today and the margin AA keeps on a B2B price after it.">
            <DataTable<PartnerTier>
              tableId="agency-tiers"
              data={tierRows}
              columns={tierColumns}
              getRowId={(r) => r.code}
              loading={tiers.isLoading}
              error={tiers.isError ? (tiers.error as Error).message : null}
              onRetry={() => tiers.refetch()}
              searchable={false}
              pageSizeOptions={[25, 50]}
              emptyTitle="No tiers"
              emptyDescription="The standard, silver and gold tiers come with the database update that adds tiers."
            />
          </Section>
          <Section title="Discount history" description="Dated discount rows of every tier. The newest approved row on or before today is in force.">
            <DataTable<TierDiscountRow>
              tableId="agency-tier-discounts"
              data={discountRows}
              columns={discountColumns}
              getRowId={(r) => r.id}
              loading={discounts.isLoading}
              error={discounts.isError ? (discounts.error as Error).message : null}
              onRetry={() => discounts.refetch()}
              searchable={false}
              pageSizeOptions={[25, 50, 100]}
              emptyTitle="No discount rows"
              emptyDescription="Add a discount to a tier; a second person approves it."
            />
          </Section>
        </>
      )}
      {tab === "agencies" && (
        <Section title="Agencies" description="Every B2B partner and what is taken off its agency price today. Changing the tier is recorded in the audit log.">
          <DataTable<PartnerTerms>
            tableId="agency-terms"
            data={partnerRows}
            columns={partnerColumns}
            getRowId={(r) => r.id}
            loading={partners.isLoading}
            error={partners.isError ? (partners.error as Error).message : null}
            onRetry={() => partners.refetch()}
            pageSizeOptions={[25, 50, 100]}
            emptyTitle="No B2B partners yet"
            emptyDescription="Agencies appear here when their Content studio tenant is projected into Booking."
          />
        </Section>
      )}
      {tab === "cards" && (
        <Section
          title="Contract rate cards"
          description="A card in force replaces the agency's tier discount; a card for one tour wins over a card for every tour. To stop a card, add an end card (End): the agency is back on its tier from that day."
        >
          <DataTable<RateCardRow>
            tableId="agency-rate-cards"
            data={cardRows}
            columns={cardColumns}
            getRowId={(r) => r.id}
            loading={cards.isLoading}
            error={cards.isError ? (cards.error as Error).message : null}
            onRetry={() => cards.refetch()}
            pageSizeOptions={[25, 50, 100]}
            emptyTitle="No rate cards"
            emptyDescription="Agencies are priced by their tier until a contract rate card is approved."
          />
        </Section>
      )}

      <ConfirmDialog
        open={approving !== null}
        onClose={() => setApproving(null)}
        onConfirm={() => approving && approve.mutate(approving)}
        busy={approve.isPending}
        error={approve.error ? approveErrorText(approve.error) : null}
        title={approving?.kind === "card" ? "Approve rate card" : "Approve tier discount"}
        confirmLabel="Approve"
        message={
          approving?.kind === "discount" ? (
            <>
              Approve <strong className="text-ink">{approving.row.tier_label}</strong> at{" "}
              <strong className="text-ink">{pct(approving.row.discount_pct)}</strong> off the agency price from{" "}
              <DateText value={approving.row.valid_from} />? Quotes priced on or after that day use it.
            </>
          ) : approving?.kind === "card" && approving.row.discount_pct === null ? (
            <>
              Approve ending the contract rate of <strong className="text-ink">{approving.row.agency_name}</strong> on{" "}
              {approving.row.source_id ? "one tour" : "every tour"} from <DateText value={approving.row.valid_from} />? From
              that day the agency&apos;s quotes use its tier discount again
              {approving.row.source_id ? " (or its card for every tour)" : ""}.
            </>
          ) : approving?.kind === "card" ? (
            <>
              Approve <strong className="text-ink">{pct(approving.row.discount_pct)}</strong> for{" "}
              <strong className="text-ink">{approving.row.agency_name}</strong> on{" "}
              {approving.row.source_id ? "one tour" : "every tour"} from <DateText value={approving.row.valid_from} />? It
              replaces the agency&apos;s tier discount while it is valid.
            </>
          ) : null
        }
      />
      {drawer === "tier" && <NewTierDrawer onClose={() => setDrawer(null)} />}
      {drawer === "discount" && <NewTierDiscountDrawer tiers={tierRows} onClose={() => setDrawer(null)} />}
      {drawer === "card" && <NewRateCardDrawer partners={partnerRows} onClose={() => setDrawer(null)} />}
      {cardPrefill && (
        <NewRateCardDrawer
          partners={partnerRows}
          initialPartner={cardPrefill.partner}
          initialTour={cardPrefill.tour}
          initialKind={cardPrefill.kind}
          onClose={() => setCardPrefill(null)}
        />
      )}
    </PageFrame>
  );
}

function personText(person: { id: string; name: string | null } | null) {
  if (!person) return <span className="text-[13px] text-muted">—</span>;
  const label = person.id.startsWith("seed") ? "Migration seed" : (person.name ?? person.id);
  return <span className="text-[13px] text-ink">{label}</span>;
}
