"use client";
// app/(staff)/customers/[id]/page.tsx — Customer detail (W1, UI spec §4.12; Figma USER  DETAILS, -1, -2).
// GET /booking/customers/{id|CUS-code}: header (name, computed status, Blocked / Merge review chips, code,
// location, added on, source), actions Edit (PATCH), New trip case, Block / Unblock and Archive (admin),
// tabs Overview (contact, consent, merge-review panel, recent activity) / Trip cases / Inquiries.

import { use, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Archive, Ban, Briefcase, CalendarDays, FileText, Hash, Inbox, MapPin, Pencil, Plane, ShieldCheck, UserPlus, Users } from "lucide-react";
import { api, ApiError } from "@/lib/api/client";
import type { CustomerDetail, TripCaseListItem } from "@/lib/api/types";
import {
  Button,
  Code,
  CustomerStatusBadge,
  DataTable,
  DateText,
  EmptyState,
  ErrorState,
  Pill,
  Section,
  TripCaseStatusBadge,
  useCan,
} from "@/ui";
import { RowMenu } from "@/ui/booking/RowMenu";
import { DetailHeader, DetailSkeleton, Fact, FactGrid, MetaItem, useDetailTab } from "@/app/(staff)/_parts/DetailHeader";
import { CustomerFormDrawer } from "../_parts/CustomerFormDrawer";
import { useCustomerActions } from "../_parts/useCustomerActions";
import { SOURCE_LABEL, customerRef } from "../_parts/shared";

export default function CustomerDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const ref = decodeURIComponent(id);
  const router = useRouter();
  const q = useQuery({ queryKey: ["customer", ref], queryFn: () => api.customers.get(ref) });
  const canWrite = useCan("customer.write");
  const canCreateCase = useCan("trip_case.write");
  const actions = useCustomerActions({ onArchived: () => router.push("/customers") });
  const [editing, setEditing] = useState(false);
  const tabs = [
    { key: "overview", label: "Overview" },
    { key: "cases", label: "Trip cases", count: q.data?.cases.length },
    { key: "inquiries", label: "Inquiries", count: q.data?.inquiries.length },
  ];
  const [tab, setTab] = useDetailTab(tabs);

  if (q.isLoading) return <DetailSkeleton label="Loading customer" />;
  if (q.isError || !q.data) {
    const status = q.error instanceof ApiError ? q.error.status : 0;
    return (
      <main className="aa-admin-main flex-1 overflow-y-auto px-8 py-6">
        {status === 404 ? (
          <EmptyState
            icon={<Users size={30} />}
            title="Customer not found"
            description="The customer may have been archived, or the link is wrong."
            action={<Link href="/customers" className="text-[13px] font-semibold text-accent-deep hover:underline">Back to customers</Link>}
          />
        ) : status === 403 ? (
          <EmptyState title="You do not have access to customers" description="Your Booking role cannot read customer records." />
        ) : (
          <ErrorState message={(q.error as Error)?.message ?? "Could not load the customer."} onRetry={() => q.refetch()} />
        )}
      </main>
    );
  }

  const c = q.data;
  const newCaseHref = `/cases/new?customer=${customerRef(c)}`;
  const adminMenu = actions.canAdmin
    ? [
        c.is_blocked
          ? { label: "Unblock customer", icon: <ShieldCheck size={15} />, onSelect: () => actions.unblock(c) }
          : { label: "Block customer", icon: <Ban size={15} />, onSelect: () => actions.block(c), danger: true },
        { label: "Archive", icon: <Archive size={15} />, onSelect: () => actions.archive(c), danger: true },
      ]
    : [];

  return (
    <main className="aa-admin-main flex-1 overflow-y-auto">
      <DetailHeader
        back={{ href: "/customers", label: "Customers" }}
        name={c.name ?? "Unnamed customer"}
        badges={
          <>
            <CustomerStatusBadge status={c.status} />
            {c.is_blocked && <Pill tone="danger" dot={false}>Blocked</Pill>}
            {c.needs_merge_review && <Pill tone="warning" dot={false}>Merge review</Pill>}
          </>
        }
        meta={
          <>
            {c.code && <MetaItem icon={<Hash size={14} />}><Code>{c.code}</Code></MetaItem>}
            {c.address && <MetaItem icon={<MapPin size={14} />}>{c.address}</MetaItem>}
            <MetaItem icon={<CalendarDays size={14} />}>Added on <DateText value={c.created_at} /></MetaItem>
            <MetaItem icon={<Inbox size={14} />}>{SOURCE_LABEL[c.source ?? ""] ?? c.source ?? "Unknown source"}</MetaItem>
          </>
        }
        actions={
          <>
            {canWrite && (
              <Button variant="secondary" onClick={() => setEditing(true)}>
                <Pencil size={14} /> Edit profile
              </Button>
            )}
            {canCreateCase && !c.is_blocked && (
              <Link
                href={newCaseHref}
                className="inline-flex items-center gap-1.5 rounded-full border border-accent bg-accent px-4 py-2 text-[13px] font-semibold uppercase tracking-[0.04em] text-on-accent hover:bg-accent-deep"
              >
                <Briefcase size={14} /> New trip case
              </Link>
            )}
            {adminMenu.length > 0 && <RowMenu items={adminMenu} label="More customer actions" />}
          </>
        }
        tabs={tabs}
        activeTab={tab}
        onTab={setTab}
      />
      <div className="mx-auto max-w-[1440px] py-6 md:px-8">
        {c.is_blocked && (
          <div role="status" className="mb-5 flex flex-wrap items-center gap-2 rounded-lg border border-danger-border bg-danger-soft px-4 py-3 text-[13px] text-danger">
            <Ban size={15} /> This customer is blocked: no new trip case can be opened for them.
            {actions.canAdmin && (
              <button onClick={() => actions.unblock(c)} className="ml-auto cursor-pointer font-semibold underline-offset-2 hover:underline">
                Unblock
              </button>
            )}
          </div>
        )}
        {tab === "overview" && <Overview c={c} />}
        {tab === "cases" && <Cases c={c} newCaseHref={canCreateCase && !c.is_blocked ? newCaseHref : null} />}
        {tab === "inquiries" && <Inquiries c={c} />}
      </div>
      <CustomerFormDrawer open={editing} onClose={() => setEditing(false)} mode={{ kind: "edit", customer: c }} />
      {actions.dialogs}
    </main>
  );
}

function Overview({ c }: { c: CustomerDetail }) {
  return (
    <div className="grid gap-5 lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
      <div className="flex min-w-0 flex-col gap-5">
        <Section title="Contact information">
          <FactGrid>
            <Fact label="E-mail address">
              <span className="flex flex-wrap items-center gap-2">
                <span className="break-all">{c.email ?? "—"}</span>
                {c.email && (c.verified ? <Pill tone="success" dot={false}>Verified</Pill> : <Pill tone="accent" dot={false}>Unverified</Pill>)}
              </span>
            </Fact>
            <Fact label="Phone number">{c.phone ?? "—"}</Fact>
            <Fact label="Location" wide>{c.address ?? "—"}</Fact>
            <Fact label="Contact consent">
              {c.contact_consent ? (
                <>Yes, given <DateText value={c.contact_consent_at} /></>
              ) : (
                <span className="text-muted">Not given</span>
              )}
            </Fact>
            <Fact label="Trip cases">
              <span className="bk-tabular">{c.case_count}</span>
              <span className="text-muted"> ({c.open_case_count} open)</span>
            </Fact>
          </FactGrid>
        </Section>
        {c.needs_merge_review && <MergePanel c={c} />}
      </div>
      <Section title="Recent activity">
        {c.recent_activity.length === 0 ? (
          <p className="text-[13px] text-muted">Nothing recorded yet.</p>
        ) : (
          <ol className="flex flex-col">
            {c.recent_activity.map((a) => (
              <li key={a.id} className="flex gap-3 border-b border-line2 py-3 first:pt-0 last:border-b-0 last:pb-0">
                <span className="grid h-8 w-8 shrink-0 place-items-center rounded-full bg-panel text-muted">
                  {a.entity_type === "trip_case" ? <FileText size={14} /> : a.action === "customer.created" ? <UserPlus size={14} /> : <Users size={14} />}
                </span>
                <span className="min-w-0">
                  <span className="block text-[13px] font-medium text-ink">
                    {a.summary ?? a.action}
                    {a.entity_type === "trip_case" && a.entity_label && <span className="font-normal text-muted"> · {a.entity_label}</span>}
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
  );
}

function MergePanel({ c }: { c: CustomerDetail }) {
  const m = c.merge_candidate;
  return (
    <Section title="Merge review" description="This lead was not merged automatically (O6). Check that it is the same person before anyone merges them.">
      {m ? (
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-warning bg-warning-soft px-4 py-3 text-[13px] text-body">
          <span>
            Shares an e-mail or phone with{" "}
            {m.archived ? (
              <span className="font-semibold text-ink">{m.name ?? m.code}</span>
            ) : (
              <Link href={`/customers/${customerRef(m)}`} className="font-semibold text-accent-deep hover:underline">
                {m.name ?? m.code ?? "another customer"}
              </Link>
            )}
            {m.code && <> (<Code>{m.code}</Code>)</>}
            {m.verified ? ", a verified customer." : ", another unverified lead."}
            {m.archived && " That customer is archived."}
          </span>
          <span className="text-xs text-muted">Merging and &ldquo;Mark reviewed&rdquo; come in wave 2.</span>
        </div>
      ) : (
        <p className="text-[13px] text-muted">Flagged for review, but the matching customer is no longer on record.</p>
      )}
    </Section>
  );
}

function Cases({ c, newCaseHref }: { c: CustomerDetail; newCaseHref: string | null }) {
  const router = useRouter();
  if (c.cases.length === 0) {
    return (
      <Section>
        <EmptyState
          icon={<Plane size={30} />}
          title="No trip cases yet"
          description="This customer has no trip case with Adventure Asia yet."
          action={
            newCaseHref && (
              <Link href={newCaseHref} className="inline-flex items-center gap-1.5 rounded-full border border-accent-border px-4 py-2 text-[12px] font-semibold uppercase tracking-[0.06em] text-accent-deep hover:bg-accent-tint">
                + Create first trip case
              </Link>
            )
          }
        />
      </Section>
    );
  }
  const columns: ColumnDef<TripCaseListItem, unknown>[] = [
    { id: "code", header: "Case", accessorKey: "code", cell: ({ row }) => <Code>{row.original.code}</Code> },
    { id: "trip", header: "Trip", accessorFn: (r) => r.trip_title ?? "", cell: ({ row }) => <span className="text-[13px] text-body">{row.original.trip_title ?? "Custom trip"}</span> },
    { id: "status", header: "Status", accessorKey: "status", cell: ({ row }) => <TripCaseStatusBadge status={row.original.status} /> },
    { id: "updated", header: "Updated", accessorKey: "updated_at", cell: ({ row }) => <DateText value={row.original.updated_at} relative className="text-[13px] text-muted" /> },
  ];
  return (
    <Section padded={false} className="p-4">
      <DataTable<TripCaseListItem>
        tableId="customer-cases"
        data={c.cases}
        columns={columns}
        getRowId={(r) => r.id}
        pageSizeOptions={[25, 50, 100]}
        emptyTitle="No trip cases yet"
        onRowClick={(r) => router.push(`/cases/${encodeURIComponent(r.code)}`)}
      />
    </Section>
  );
}

function Inquiries({ c }: { c: CustomerDetail }) {
  if (c.inquiries.length === 0) {
    return (
      <Section>
        <EmptyState
          icon={<Inbox size={30} />}
          title="No TripPlanner inquiries"
          description="Trip cases this customer opened from TripPlanner are listed here."
        />
      </Section>
    );
  }
  return (
    <Section title="Inquiries from TripPlanner" description="Each inquiry is a trip case; New inquiry means nobody has picked it up yet.">
      <div className="grid gap-3">
        {c.inquiries.map((i) => (
          <Link
            key={i.id}
            href={`/cases/${encodeURIComponent(i.code)}`}
            className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-line bg-card px-4 py-3.5 hover:border-accent-border"
          >
            <span className="min-w-0">
              <span className="block text-[13px] font-medium text-ink">{i.trip_title ?? "Custom trip"}</span>
              <span className="text-xs text-muted">
                <Code>{i.code}</Code> · {i.leg_titles.length} {i.leg_titles.length === 1 ? "leg" : "legs"}
                {i.duration_days ? ` · ${i.duration_days} days` : ""} · received <DateText value={i.created_at} />
              </span>
            </span>
            <TripCaseStatusBadge status={i.status} />
          </Link>
        ))}
      </div>
    </Section>
  );
}
