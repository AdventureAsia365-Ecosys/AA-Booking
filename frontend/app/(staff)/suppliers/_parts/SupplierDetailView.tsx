"use client";
// app/(staff)/suppliers/_parts/SupplierDetailView.tsx — Supplier detail (W2, AA-822; module spec 08 screen 4 + conflict
// 7): header (name, status, code, categories, location), actions Edit profile / Deactivate | Activate (supply.write),
// tabs Profile (basic information, contact, location, recent activity from the audit log) and Contracts (draft →
// published by a second person → expired) and Rates (net rates per contract, AA-823 bk-010; rate-sheet upload is a later
// slice) and Bookings (AA-682 S1, supplier bookings of sold trips) and Bills (AA-682 S4, bk-030: this supplier's bills,
// the bill drawer and payouts; bill.read). Reads need supply.read.

import { useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Ban, CalendarDays, CheckCircle2, FileText, Hash, MapPin, Pencil, Plus, Power, Store, Tag } from "lucide-react";
import { ApiError } from "@/lib/api/client";
import { suppliersApi, type SupplierContract, type SupplierDetail } from "@/lib/api/suppliers";
import { Button, Code, ConfirmDialog, DataTable, DateText, EmptyState, ErrorState, FromCisTag, Section, useStaff, useToast } from "@/ui";
import { RowMenu, type RowMenuItem } from "@/ui/booking/RowMenu";
import { DetailHeader, DetailSkeleton, Fact, FactGrid, MetaItem, useDetailTab } from "@/app/(staff)/_parts/DetailHeader";
import { ContractDrawer } from "./ContractDrawer";
import { RatesTab } from "./RatesTab";
import { SupplierBookingsTab } from "./SupplierBookingsTab";
import { SupplierBillsList } from "@/app/(staff)/billing/_parts/SupplierBills";
import { CATEGORY_LABEL, ContractPhaseBadge, SupplierStatusBadge, locationText, supplierError, supplierHref } from "./shared";
import { useSupplierActions } from "./useSupplierActions";

export function SupplierDetailView({ refParam }: { refParam: string }) {
  const ref = decodeURIComponent(refParam);
  const q = useQuery({ queryKey: ["supplier", ref], queryFn: () => suppliersApi.get(ref) });
  const actions = useSupplierActions();
  const tabs = [
    { key: "profile", label: "Profile" },
    { key: "contracts", label: "Contracts", count: q.data?.contract_counts.total },
    { key: "rates", label: "Rates" },
    { key: "bookings", label: "Bookings" },
    { key: "bills", label: "Bills" },
  ];
  const [tab, setTab] = useDetailTab(tabs);

  if (q.isLoading) return <DetailSkeleton label="Loading supplier" />;
  if (q.isError || !q.data) {
    const status = q.error instanceof ApiError ? q.error.status : 0;
    return (
      <main className="aa-admin-main flex-1 overflow-y-auto px-8 py-6">
        {status === 404 ? (
          <EmptyState
            icon={<Store size={30} />}
            title="Supplier not found"
            description="The link may be wrong, or the code was mistyped."
            action={<Link href="/suppliers" className="text-[13px] font-semibold text-accent-deep hover:underline">Back to suppliers</Link>}
          />
        ) : status === 403 ? (
          <EmptyState title="You do not have access to suppliers" description="Your Booking role cannot read supplier records." />
        ) : (
          <ErrorState message={(q.error as Error)?.message ?? "Could not load the supplier."} onRetry={() => q.refetch()} />
        )}
      </main>
    );
  }

  const s = q.data;
  const where = locationText(s);
  return (
    <main className="aa-admin-main flex-1 overflow-y-auto">
      <DetailHeader
        back={{ href: "/suppliers", label: "Suppliers" }}
        name={s.company_name}
        badges={<SupplierStatusBadge status={s.status} />}
        meta={
          <>
            <MetaItem icon={<Hash size={14} />}>
              <Code>{s.code}</Code>
            </MetaItem>
            <MetaItem icon={<Tag size={14} />}>{s.categories.map((c) => CATEGORY_LABEL[c] ?? c).join(", ")}</MetaItem>
            {where && <MetaItem icon={<MapPin size={14} />}>{where}</MetaItem>}
            <MetaItem icon={<CalendarDays size={14} />}>
              Added on <DateText value={s.created_at} />
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
                <Pencil size={14} /> Edit profile
              </Link>
              {s.status === "active" ? (
                <Button variant="secondary" onClick={() => actions.deactivate(s)}>
                  <Ban size={14} /> Deactivate
                </Button>
              ) : (
                <Button variant="secondary" onClick={() => actions.activate(s)}>
                  <Power size={14} /> Activate
                </Button>
              )}
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
            <Ban size={15} /> This supplier is inactive: it stays on record, but no new contract can be added or published.
          </div>
        )}
        {tab === "profile" && <Profile s={s} />}
        {tab === "contracts" && <Contracts s={s} canWrite={actions.canWrite} />}
        {tab === "rates" && <RatesTab s={s} canWrite={actions.canWrite} />}
        {tab === "bookings" && <SupplierBookingsTab s={s} />}
        {tab === "bills" && (
          <SupplierBillsList
            supplier={{ id: s.id, name: s.company_name }}
            title="Bills from this supplier"
            description="Each bill is matched to confirmed bookings with this supplier, approved by a second person, then paid out by an admin."
          />
        )}
      </div>
      {actions.dialogs}
    </main>
  );
}

function Profile({ s }: { s: SupplierDetail }) {
  return (
    <div className="grid gap-5 lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
      <div className="flex min-w-0 flex-col gap-5">
        <Section title="Basic information">
          <FactGrid>
            <Fact label="Categories">{s.categories.map((c) => CATEGORY_LABEL[c] ?? c).join(", ")}</Fact>
            <Fact label="Website">
              {s.website ? (
                <a href={s.website} target="_blank" rel="noopener noreferrer" className="break-all text-accent-deep hover:underline">
                  {s.website}
                </a>
              ) : (
                "—"
              )}
            </Fact>
            <Fact label="Internal notes" wide>
              {s.internal_notes ? <span className="whitespace-pre-line">{s.internal_notes}</span> : <span className="text-muted">None</span>}
            </Fact>
          </FactGrid>
        </Section>
        <Section title="Location">
          <FactGrid>
            <Fact label="Street address" wide>
              {s.address ?? "—"}
            </Fact>
            <Fact label="City / place">
              {s.destination ? (
                <span className="inline-flex flex-wrap items-center gap-2">
                  {s.destination.name} <FromCisTag />
                </span>
              ) : s.destination_id ? (
                <span className="text-muted">Set, but the CIS catalog cannot be read right now</span>
              ) : (
                "—"
              )}
            </Fact>
            <Fact label="Country">{s.country_name ?? "—"}</Fact>
            <Fact label="State / province">{s.region ?? "—"}</Fact>
            <Fact label="Postal code">{s.postal_code ?? "—"}</Fact>
          </FactGrid>
        </Section>
      </div>
      <div className="flex min-w-0 flex-col gap-5">
        <Section title="Contact details">
          <FactGrid>
            <Fact label="Primary contact">{s.contact_name ?? "—"}</Fact>
            <Fact label="Job title">{s.contact_title ?? "—"}</Fact>
            <Fact label="E-mail">
              {s.contact_email ? (
                <a href={`mailto:${s.contact_email}`} className="break-all text-accent-deep hover:underline">
                  {s.contact_email}
                </a>
              ) : (
                "—"
              )}
            </Fact>
            <Fact label="Phone">{s.contact_phone ?? "—"}</Fact>
          </FactGrid>
        </Section>
        <Section title="Recent activity">
          {s.recent_activity.length === 0 ? (
            <p className="text-[13px] text-muted">Nothing recorded yet.</p>
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
                      {(a.entity_type === "supplier_contract" || a.entity_type === "supplier_rate") && a.entity_label && <span className="font-normal text-muted"> · {a.entity_label}</span>}
                    </span>
                    <span className="text-xs text-muted">
                      {a.actor.name ?? (a.actor.id ? "Staff" : "System")} · <DateText value={a.at} withTime />
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

function Contracts({ s, canWrite }: { s: SupplierDetail; canWrite: boolean }) {
  const me = useStaff();
  const qc = useQueryClient();
  const toast = useToast();
  const list = useQuery({ queryKey: ["supplier", s.code, "contracts"], queryFn: () => suppliersApi.contracts(s.code) });
  const [drawer, setDrawer] = useState<{ contract?: SupplierContract } | null>(null);
  const [move, setMove] = useState<Move>(null);

  const run = useMutation({
    mutationFn: (m: NonNullable<Move>) =>
      suppliersApi.updateContract(m.contract.id, { status: m.kind === "publish" ? "published" : "expired" }),
    onSuccess: (k, m) => {
      qc.invalidateQueries({ queryKey: ["supplier"] });
      qc.invalidateQueries({ queryKey: ["suppliers"] });
      toast.success(m.kind === "publish" ? `${k.title} published.` : `${k.title} expired.`);
      setMove(null);
    },
  });

  function ask(m: NonNullable<Move>) {
    run.reset();
    setMove(m);
  }

  /** The maker of a draft is its last writer (bk-008): they cannot publish it. */
  const isMaker = (k: SupplierContract) => (k.updated_by ?? k.created_by) === me.cis_user_id;

  function menuFor(k: SupplierContract): RowMenuItem[] {
    if (!canWrite) return [];
    if (k.status === "draft") {
      const own = isMaker(k);
      return [
        { label: "Edit draft", icon: <Pencil size={15} />, onSelect: () => setDrawer({ contract: k }) },
        {
          label: own ? "Publish (needs a second person)" : "Publish",
          icon: <CheckCircle2 size={15} />,
          onSelect: () => ask({ kind: "publish", contract: k }),
          disabled: own || s.status !== "active",
        },
      ];
    }
    if (k.status === "published") {
      return [{ label: "Expire", icon: <Ban size={15} />, onSelect: () => ask({ kind: "expire", contract: k }), danger: true }];
    }
    return [];
  }

  // AA-910 (S250): title 200 px, terms 120 px and "Entered by" 140 px on desktop (full text in the tooltips), so the
  // contracts table keeps 20 px to spare at 1400 px.
  const columns: ColumnDef<SupplierContract, unknown>[] = [
    {
      id: "title",
      header: "Contract",
      accessorKey: "title",
      cell: ({ row }) => (
        <span className="flex min-w-0 max-w-[200px] flex-col">
          <span className="truncate text-[13.5px] font-medium text-ink" title={row.original.title}>
            {row.original.title}
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
      header: "Validity",
      accessorKey: "valid_from",
      cell: ({ row }) => (
        <span className="flex flex-col whitespace-nowrap text-[13px] text-body">
          <DateText value={row.original.valid_from} />
          <span className="text-xs text-muted">
            to {row.original.valid_to ? <DateText value={row.original.valid_to} /> : "open-ended"}
          </span>
        </span>
      ),
    },
    { id: "currency", header: "Currency", accessorKey: "currency", cell: ({ row }) => <Code>{row.original.currency}</Code> },
    {
      id: "terms",
      header: "Payment terms",
      enableSorting: false,
      cell: ({ row }) => (
        <span className="block break-words text-[13px] text-body md:max-w-[120px] md:truncate" title={row.original.payment_terms ?? undefined}>
          {row.original.payment_terms ?? "—"}
        </span>
      ),
    },
    { id: "phase", header: "Status", accessorKey: "phase", cell: ({ row }) => <ContractPhaseBadge phase={row.original.phase} /> },
    {
      id: "who",
      header: "Entered by",
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
              <span className="md:truncate" title={`Published by ${published}`}>
                Published by {published}
              </span>
            )}
          </span>
        );
      },
    },
    {
      id: "actions",
      header: "Actions",
      enableSorting: false,
      cell: ({ row }) => {
        const items = menuFor(row.original);
        return items.length ? <RowMenu items={items} label={`Actions for ${row.original.title}`} /> : null;
      },
    },
  ];

  const k = move?.contract;
  return (
    <Section
      padded={false}
      className="p-4"
      title="Contracts"
      description="Terms per period in the supplier's currency. A draft is published by a second person; published terms never change."
      actions={
        canWrite &&
        s.status === "active" && (
          <Button variant="primary" onClick={() => setDrawer({})}>
            <Plus size={15} /> New contract
          </Button>
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
          emptyTitle="No contracts yet"
          emptyDescription={canWrite ? "Add the supplier's terms with New contract." : "Contracting adds the supplier's contracts here."}
          tableMinWidth={820}
        />
      </div>
      {drawer && <ContractDrawer supplierRef={s.code} contract={drawer.contract} onClose={() => setDrawer(null)} />}
      <ConfirmDialog
        open={move !== null}
        onClose={() => setMove(null)}
        onConfirm={() => move && run.mutate(move)}
        busy={run.isPending}
        error={supplierError(run.error)}
        danger={move?.kind === "expire"}
        title={move?.kind === "publish" ? "Publish contract" : "Expire contract"}
        confirmLabel={move?.kind === "publish" ? "Publish" : "Expire"}
        message={
          move?.kind === "publish" ? (
            <>
              Publish <strong className="text-ink">{k?.title}</strong> ({k?.currency})? You confirm you checked the terms entered by{" "}
              {k?.updated_by_name ?? k?.created_by_name ?? "another staff member"}. Once published the contract is fixed.
            </>
          ) : (
            <>
              Expire <strong className="text-ink">{k?.title}</strong>? It closes for good and stops counting as in force. New terms are a
              new contract.
            </>
          )
        }
      />
    </Section>
  );
}
