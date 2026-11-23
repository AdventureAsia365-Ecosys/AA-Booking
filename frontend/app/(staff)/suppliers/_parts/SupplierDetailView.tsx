"use client";
// app/(staff)/suppliers/_parts/SupplierDetailView.tsx — Supplier detail (W2, AA-822; module spec 08 screen 4 + conflict
// 7): header (name, status, code, categories, location), actions Edit profile / Deactivate | Activate (supply.write),
// tabs Profile (basic information, contact, location, recent activity from the audit log) and Contracts (draft →
// published by a second person → expired; "Import rate sheet" fills a draft from the Excel template, AA-1029) and Rates
// (net rates per contract, AA-823 bk-010) and Bookings (AA-682 S1, supplier bookings of sold trips) and Bills (AA-682
// S4, bk-030: this supplier's bills, the bill drawer and payouts; bill.read). Reads need supply.read.

import { useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Ban, CalendarDays, CheckCircle2, FileSpreadsheet, FileText, Hash, MapPin, Pencil, Plus, Power, Store, Tag } from "lucide-react";
import { ApiError } from "@/lib/api/client";
import { suppliersApi, type SupplierContract, type SupplierDetail } from "@/lib/api/suppliers";
import { Button, Code, ConfirmDialog, DataTable, PageFrame, DateText, EmptyState, ErrorState, FromCisTag, Section, useCan, useStaff, useToast, RowMenu, type RowMenuItem } from "@/ui";
import { DetailHeader, DetailSkeleton, Fact, FactGrid, MetaItem, useDetailTab } from "@/app/(staff)/_parts/DetailHeader";
import { ContractDrawer } from "./ContractDrawer";
import { RateSheetDrawer } from "./RateSheetDrawer";
import { RatesTab } from "./RatesTab";
import { SupplierBookingsTab } from "./SupplierBookingsTab";
import { SupplierBillsList } from "@/app/(staff)/billing/_parts/SupplierBills";
import { CATEGORY_LABEL, ContractPhaseBadge, SupplierStatusBadge, locationText, supplierError, supplierHref } from "./shared";
import { useSupplierActions } from "./useSupplierActions";
import { useT } from "@/ui/core/useI18n";
import { useTx } from "@/ui/local/useTx";
import { MSG } from "./SupplierDetailView.messages";

export function SupplierDetailView({ refParam }: { refParam: string }) {
  const t = useT(MSG);
  const tx = useTx();
  const ref = decodeURIComponent(refParam);
  const q = useQuery({ queryKey: ["supplier", ref], queryFn: () => suppliersApi.get(ref) });
  const actions = useSupplierActions();
  const tabs = [
    { key: "profile", label: t("Profile") },
    { key: "contracts", label: t("Contracts"), count: q.data?.contract_counts.total },
    { key: "rates", label: t("Rates") },
    { key: "bookings", label: t("Bookings") },
    { key: "bills", label: t("Bills") },
  ];
  const [tab, setTab] = useDetailTab(tabs);

  if (q.isLoading) return <DetailSkeleton label={t("Loading supplier")} />;
  if (q.isError || !q.data) {
    const status = q.error instanceof ApiError ? q.error.status : 0;
    return (
      <PageFrame width="full">
        {status === 404 ? (
          <EmptyState
            icon={<Store size={30} />}
            title={t("Supplier not found")}
            description={t("The link may be wrong, or the code was mistyped.")}
            action={<Link href="/suppliers" className="text-[13px] font-semibold text-accent-deep hover:underline">{t("Back to suppliers")}</Link>}
          />
        ) : status === 403 ? (
          <EmptyState title={t("You do not have access to suppliers")} description={t("Your Booking role cannot read supplier records.")} />
        ) : (
          <ErrorState message={(q.error as Error)?.message ?? t("Could not load the supplier.")} onRetry={() => q.refetch()} />
        )}
      </PageFrame>
    );
  }

  const s = q.data;
  const where = locationText(s);
  return (
    <PageFrame bleed>
      <DetailHeader
        back={{ href: "/suppliers", label: t("Suppliers") }}
        name={s.company_name}
        badges={<SupplierStatusBadge status={s.status} />}
        meta={
          <>
            <MetaItem icon={<Hash size={14} />}>
              <Code>{s.code}</Code>
            </MetaItem>
            <MetaItem icon={<Tag size={14} />}>{s.categories.map((c) => tx(CATEGORY_LABEL[c] ?? c)).join(", ")}</MetaItem>
            {where && <MetaItem icon={<MapPin size={14} />}>{where}</MetaItem>}
            <MetaItem icon={<CalendarDays size={14} />}>
              {t("Added on")} <DateText value={s.created_at} />
            </MetaItem>
          </>
        }
        actions={
          actions.canWrite && (
            <>
              <Link
                href={supplierHref(s, "/edit")}
                className="inline-flex items-center gap-1.5 rounded-full border border-line bg-card px-4 py-2 text-[13px] font-semibold text-ink hover:border-accent-border"
              >
                <Pencil size={14} /> {t("Edit profile")}
              </Link>
              <StatusButton s={s} actions={actions} />
            </>
          )
        }
        tabs={tabs}
        activeTab={tab}
        onTab={setTab}
      />
      <div className="mx-auto max-w-[1440px] py-6 md:px-8">
        {s.status === "inactive" && (
          <div role="status" className="mb-5 flex flex-wrap items-center gap-2 rounded-lg border border-line bg-panel px-4 py-3 text-[13px] text-body">
            <Ban size={15} /> {t("This supplier is inactive: it stays on record, but no new contract can be added or published.")}
          </div>
        )}
        {s.status === "pending" && (
          <div role="status" className="mb-5 flex flex-wrap items-center gap-2 rounded-lg border border-line bg-panel px-4 py-3 text-[13px] text-body">
            <Power size={15} /> {t("This supplier waits for approval: a second person activates it before contracts can be added.")}
          </div>
        )}
        {tab === "profile" && <Profile s={s} />}
        {tab === "contracts" && <Contracts s={s} canWrite={actions.canWrite} />}
        {tab === "rates" && <RatesTab s={s} canWrite={actions.canWrite} />}
        {tab === "bookings" && <SupplierBookingsTab s={s} />}
        {tab === "bills" && (
          <SupplierBillsList
            supplier={{ id: s.id, name: s.company_name }}
            title={t("Bills from this supplier")}
            description={t("Each bill is matched to confirmed bookings with this supplier, approved by a second person, then paid out by an admin.")}
          />
        )}
      </div>
      {actions.dialogs}
    </PageFrame>
  );
}

function Profile({ s }: { s: SupplierDetail }) {
  const t = useT(MSG);
  const tx = useTx();
  return (
    <div className="grid gap-5 lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
      <div className="flex min-w-0 flex-col gap-5">
        <Section title={t("Basic information")}>
          <FactGrid>
            <Fact label={t("Categories")}>{s.categories.map((c) => tx(CATEGORY_LABEL[c] ?? c)).join(", ")}</Fact>
            <Fact label={t("Website")}>
              {s.website ? (
                <a href={s.website} target="_blank" rel="noopener noreferrer" className="break-all text-accent-deep hover:underline">
                  {s.website}
                </a>
              ) : (
                "—"
              )}
            </Fact>
            <Fact label={t("Internal notes")} wide>
              {s.internal_notes ? <span className="whitespace-pre-line">{tx(s.internal_notes)}</span> : <span className="text-muted">{t("None")}</span>}
            </Fact>
          </FactGrid>
        </Section>
        <Section title={t("Location")}>
          <FactGrid>
            <Fact label={t("Street address")} wide>
              {s.address ?? "—"}
            </Fact>
            <Fact label={t("City / place")}>
              {s.destination ? (
                <span className="inline-flex flex-wrap items-center gap-2">
                  {s.destination.name} <FromCisTag />
                </span>
              ) : s.destination_id ? (
                <span className="text-muted">{t("Set, but the CIS catalog cannot be read right now")}</span>
              ) : (
                "—"
              )}
            </Fact>
            <Fact label={t("Country")}>{s.country_name ?? "—"}</Fact>
            <Fact label={t("State / province")}>{s.region ?? "—"}</Fact>
            <Fact label={t("Postal code")}>{s.postal_code ?? "—"}</Fact>
          </FactGrid>
        </Section>
      </div>
      <div className="flex min-w-0 flex-col gap-5">
        <Section title={t("Contact details")}>
          <FactGrid>
            <Fact label={t("Primary contact")}>{s.contact_name ?? "—"}</Fact>
            <Fact label={t("Job title")}>{s.contact_title ?? "—"}</Fact>
            <Fact label="E-mail">
              {s.contact_email ? (
                <a href={`mailto:${s.contact_email}`} className="break-all text-accent-deep hover:underline">
                  {s.contact_email}
                </a>
              ) : (
                "—"
              )}
            </Fact>
            <Fact label={t("Phone")}>{s.contact_phone ?? "—"}</Fact>
          </FactGrid>
        </Section>
        <Section title={t("Recent activity")}>
          {s.recent_activity.length === 0 ? (
            <p className="text-[13px] text-muted">{t("Nothing recorded yet.")}</p>
          ) : (
            <ol className="flex flex-col">
              {s.recent_activity.map((a) => (
                <li key={a.id} className="flex gap-3 border-b border-line2 py-3 first:pt-0 last:border-b-0 last:pb-0">
                  <span className="grid h-8 w-8 shrink-0 place-items-center rounded-full bg-panel text-muted">
                    {a.entity_type === "supplier_contract" ? <FileText size={14} /> : a.entity_type === "supplier_rate" ? <Tag size={14} /> : <Store size={14} />}
                  </span>
                  <span className="min-w-0">
                    <span className="block text-[13px] font-medium text-ink">
                      {a.summary ?? a.action}
                      {(a.entity_type === "supplier_contract" || a.entity_type === "supplier_rate") && a.entity_label && <span className="font-normal text-muted"> · {tx(a.entity_label)}</span>}
                    </span>
                    <span className="text-xs text-muted">
                      {a.actor.name ?? (a.actor.id ? t("Staff") : t("System"))} · <DateText value={a.at} withTime />
                    </span>
                  </span>
                </li>
              ))}
            </ol>
          )}
        </Section>
      </div>
    </div>
  );
}

type Move = { kind: "publish" | "expire"; contract: SupplierContract } | null;

/** Deactivate an active supplier (supply.write) or activate an inactive one (AA-1150: supplier.activate, an approval). */
function StatusButton({ s, actions }: { s: SupplierDetail; actions: ReturnType<typeof useSupplierActions> }) {
  const t = useT(MSG);
  if (s.status === "active") {
    return (
      <Button variant="secondary" onClick={() => actions.deactivate(s)}>
        <Ban size={14} /> {t("Deactivate")}
      </Button>
    );
  }
  if (!actions.canActivate) return null;
  // AA-1158: the API says whether the caller may activate it (never its creator, but the business owner, flagged).
  const can = s.actions?.activate;
  const blocked = can ? !can.allowed : false;
  return (
    <Button
      variant="secondary"
      disabled={blocked}
      title={blocked ? t("A second person activates a supplier you created.") : undefined}
      onClick={() => actions.activate({ ...s, selfApproval: can?.self_approval })}
    >
      <Power size={14} /> {blocked ? t("Activate (needs a second person)") : t("Activate")}
    </Button>
  );
}

function Contracts({ s, canWrite }: { s: SupplierDetail; canWrite: boolean }) {
  const t = useT(MSG);
  const tx = useTx();
  const me = useStaff();
  const canPublish = useCan("contract.publish"); // AA-1150: publishing is an approval, not a write
  const qc = useQueryClient();
  const toast = useToast();
  const list = useQuery({ queryKey: ["supplier", s.code, "contracts"], queryFn: () => suppliersApi.contracts(s.code) });
  const [drawer, setDrawer] = useState<{ contract?: SupplierContract } | null>(null);
  const [importing, setImporting] = useState(false);
  const [move, setMove] = useState<Move>(null);

  const run = useMutation({
    mutationFn: (m: NonNullable<Move>) =>
      suppliersApi.updateContract(m.contract.id, { status: m.kind === "publish" ? "published" : "expired" }),
    onSuccess: (k, m) => {
      qc.invalidateQueries({ queryKey: ["supplier"] });
      qc.invalidateQueries({ queryKey: ["suppliers"] });
      toast.success(m.kind === "publish" ? t("{0} published.", k.title) : t("{0} expired.", k.title));
      setMove(null);
    },
  });

  function ask(m: NonNullable<Move>) {
    run.reset();
    setMove(m);
  }

  /** AA-1158: every writer of a draft is a maker (the API lists them and says whether the caller may publish). */
  const isMaker = (k: SupplierContract) =>
    k.actions?.publish ? !k.actions.publish.allowed : (k.writers ?? [k.updated_by ?? k.created_by]).includes(me.cis_user_id);

  function menuFor(k: SupplierContract): RowMenuItem[] {
    if (!canWrite) return [];
    if (k.status === "draft") {
      const own = isMaker(k);
      const items: RowMenuItem[] = [
        { label: t("Edit draft"), icon: <Pencil size={15} />, onSelect: () => setDrawer({ contract: k }) },
      ];
      if (canPublish) {
        items.push({
          label: own ? t("Publish (needs a second person)") : t("Publish"),
          icon: <CheckCircle2 size={15} />,
          onSelect: () => ask({ kind: "publish", contract: k }),
          disabled: own || s.status !== "active",
        });
      }
      return items;
    }
    if (k.status === "published") {
      return [{ label: t("Expire"), icon: <Ban size={15} />, onSelect: () => ask({ kind: "expire", contract: k }), danger: true }];
    }
    return [];
  }

  // AA-910 (S250): title 200 px, terms 120 px and "Entered by" 140 px on desktop (full text in the tooltips), so the
  // contracts table keeps 20 px to spare at 1400 px.
  const columns: ColumnDef<SupplierContract, unknown>[] = [
    {
      id: "title",
      header: t("Contract"),
      accessorKey: "title",
      cell: ({ row }) => (
        <span className="flex min-w-0 max-w-[200px] flex-col">
          <span className="truncate text-[13.5px] font-medium text-ink" title={tx(row.original.title)}>
            {tx(row.original.title)}
          </span>
          {row.original.reference && (
            <span className="truncate text-xs text-muted" title={row.original.reference}>
              {row.original.reference}
            </span>
          )}
        </span>
      ),
    },
    {
      id: "validity",
      header: t("Validity"),
      accessorKey: "valid_from",
      cell: ({ row }) => (
        <span className="flex flex-col whitespace-nowrap text-[13px] text-body">
          <DateText value={row.original.valid_from} />
          <span className="text-xs text-muted">
            {t("to")} {row.original.valid_to ? <DateText value={row.original.valid_to} /> : t("open-ended")}
          </span>
        </span>
      ),
    },
    { id: "currency", header: t("Currency"), accessorKey: "currency", cell: ({ row }) => <Code>{row.original.currency}</Code> },
    {
      id: "terms",
      header: t("Payment terms"),
      enableSorting: false,
      cell: ({ row }) => (
        <span className="block break-words text-[13px] text-body md:max-w-[120px] md:truncate" title={row.original.payment_terms ?? undefined}>
          {row.original.payment_terms ?? "—"}
        </span>
      ),
    },
    { id: "phase", header: t("Status"), accessorKey: "phase", cell: ({ row }) => <ContractPhaseBadge phase={row.original.phase} /> },
    {
      id: "who",
      header: t("Entered by"),
      enableSorting: false,
      cell: ({ row }) => {
        const entered = row.original.updated_by_name ?? row.original.created_by_name ?? "—";
        const published = row.original.published_by ? (row.original.published_by_name ?? "—") : null;
        return (
          <span className="flex min-w-0 flex-col text-xs text-muted md:max-w-[140px]">
            <span className="text-[13px] text-body md:truncate" title={entered}>
              {entered}
            </span>
            {published && (
              <span className="md:truncate" title={t("Published by {0}", published)}>
                {t("Published by {0}", published)}
              </span>
            )}
          </span>
        );
      },
    },
    {
      id: "actions",
      header: t("Actions"),
      enableSorting: false,
      cell: ({ row }) => {
        const items = menuFor(row.original);
        return items.length ? <RowMenu items={items} label={t("Actions for {0}", row.original.title)} /> : null;
      },
    },
  ];

  const k = move?.contract;
  return (
    <Section
      padded={false}
      className="p-4"
      title={t("Contracts")}
      description={t("Terms per period in the supplier's currency. A draft is published by a second person; published terms never change.")}
      actions={
        canWrite &&
        s.status === "active" && (
          <span className="flex flex-wrap gap-2">
            <Button variant="secondary" onClick={() => setImporting(true)}>
              <FileSpreadsheet size={15} /> {t("Import rate sheet")}
            </Button>
            <Button variant="primary" onClick={() => setDrawer({})}>
              <Plus size={15} /> {t("New contract")}
            </Button>
          </span>
        )
      }
    >
      <div className="bk-dt-sticky-end">
        <DataTable<SupplierContract>
          searchable={false}
          tableId="supplier-contracts"
          data={list.data?.items ?? []}
          columns={columns}
          getRowId={(r) => r.id}
          loading={list.isLoading}
          error={list.isError ? (list.error as Error).message : null}
          onRetry={() => list.refetch()}
          pageSizeOptions={[25, 50, 100]}
          emptyTitle={t("No contracts yet")}
          emptyDescription={canWrite ? t("Add the supplier's terms with New contract.") : t("Contracting adds the supplier's contracts here.")}
          tableMinWidth={820}
        />
      </div>
      {drawer && <ContractDrawer supplierRef={s.code} contract={drawer.contract} onClose={() => setDrawer(null)} />}
      {importing && <RateSheetDrawer supplierRef={s.code} onClose={() => setImporting(false)} />}
      <ConfirmDialog
        open={move !== null}
        onClose={() => setMove(null)}
        onConfirm={() => move && run.mutate(move)}
        busy={run.isPending}
        error={tx(supplierError(run.error))}
        danger={move?.kind === "expire"}
        title={move?.kind === "publish" ? t("Publish contract") : t("Expire contract")}
        confirmLabel={move?.kind === "publish" ? t("Publish") : t("Expire")}
        message={
          move?.kind === "publish" ? (
            <>
              {t("Publish")} <strong className="text-ink">{tx(k?.title)}</strong> ({k?.currency}{t(")? You confirm you checked the terms entered by {0}. Once published the contract is fixed.", k?.updated_by_name ?? k?.created_by_name ?? t("another staff member"))}
            </>
          ) : (
            <>
              {t("Expire")} <strong className="text-ink">{tx(k?.title)}</strong>{t("? It closes for good and stops counting as in force. New terms are a new contract.")}
            </>
          )
        }
      />
    </Section>
  );
}
