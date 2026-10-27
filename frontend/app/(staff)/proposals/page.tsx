"use client";
// app/(staff)/proposals/page.tsx — Cross-case proposal versions (W1, UI spec §4.11; Figma ITINERARY BUILDER,
// -1). GET /booking/versions with server pagination; filters in the URL.
// Row actions: Open builder, Preview, New version (POST …/versions: copies the case's LATEST version, the
// only copy the API offers), Send (shared send modal), Delete draft (DELETE …/versions/{n}).
// "+ New proposal": a case picker (GET /booking/trip-cases?q=) that opens the case's latest version, or
// creates V1 when the case has none.
// Layout (AA-683 S239): the table fits a 1440 px screen without sideways scroll. Proposal (V-number + title, case
// code + customer) and Trip (first leg, dates + length) are the flexible columns, one truncated line per row with the
// full text in the tooltip; the fixed columns keep their natural width. Version and Dates are no longer columns of
// their own: they sit in those two cells.

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { CopyPlus, Eye, Pencil, Plus, Search, Send, Trash2 } from "lucide-react";
import { api } from "@/lib/api/client";
import { proposalsApi } from "@/lib/api/proposals";
import type { VersionListItem } from "@/lib/api/types";
import { PAGE_SIZES, useOffsetPage } from "@/lib/useOffsetPage";
import {
  Button,
  Code,
  ConfirmDialog,
  DataTable,
  DateText,
  FilterBar,
  Modal,
  Money,
  PageFrame,
  Section,
  Spinner,
  TripCaseStatusBadge,
  VERSION_BADGE_META,
  VersionBadgePill,
  csvMoney,
  formatDate,
  useCan,
  useToast,
  useUrlFilters,
} from "@/ui";
import { SendProposalModal } from "./_parts/SendProposalModal";

const CLOSED = ["completed", "cancelled"];
const SENDABLE_FROM = ["discovery", "building", "proposal_sent"];
const builderHref = (r: { trip_case_code: string; version_number: number }) =>
  `/cases/${encodeURIComponent(r.trip_case_code)}/proposals/${r.version_number}`;

type RowAction = { kind: "copy" | "send" | "delete"; row: VersionListItem } | null;

export default function ProposalsPage() {
  const router = useRouter();
  const qc = useQueryClient();
  const toast = useToast();
  const canWrite = useCan("version.write");
  const [action, setAction] = useState<RowAction>(null);
  const [pickerOpen, setPickerOpen] = useState(false);
  const { get } = useUrlFilters();
  const paging = useOffsetPage();
  const filters = {
    q: get("q") || undefined,
    badge: get("badge") || undefined,
    assignee: get("assignee") || undefined,
    updated_from: get("from") || undefined,
    updated_to: get("to") || undefined,
    sort: "-updated_at",
    limit: paging.limit,
    offset: paging.offset,
  };
  const list = useQuery({ queryKey: ["versions", "list", filters], queryFn: () => api.versions.list(filters) });

  const copy = useMutation({
    mutationFn: (r: VersionListItem) => proposalsApi.create(r.trip_case_code),
    onSuccess: async (v, r) => {
      await Promise.all([qc.invalidateQueries({ queryKey: ["versions"] }), qc.invalidateQueries({ queryKey: ["trip-case", r.trip_case_code] })]);
      toast.success(`${r.trip_case_code} V${v.version_number} created`);
      setAction(null);
      router.push(builderHref({ trip_case_code: r.trip_case_code, version_number: v.version_number }));
    },
  });
  const remove = useMutation({
    mutationFn: (r: VersionListItem) => proposalsApi.remove(r.trip_case_code, r.version_number),
    onSuccess: async (_, r) => {
      await Promise.all([qc.invalidateQueries({ queryKey: ["versions"] }), qc.invalidateQueries({ queryKey: ["trip-case", r.trip_case_code] })]);
      toast.success(`Draft ${r.trip_case_code} V${r.version_number} deleted`);
      setAction(null);
    },
  });

  const columns = useMemo<ColumnDef<VersionListItem, unknown>[]>(
    () => [
      ...COLUMNS,
      {
        id: "actions",
        header: () => <span className="sr-only">Actions</span>,
        cell: ({ row }) => (
          <RowActions
            row={row.original}
            canWrite={canWrite}
            onOpen={(href) => router.push(href)}
            onAction={(kind) => {
              copy.reset();
              remove.reset();
              setAction({ kind, row: row.original });
            }}
          />
        ),
      },
    ],
    // copy / remove are stable mutation objects; reset() is all the cell uses.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [canWrite, router],
  );

  const target = action?.row;
  return (
    <PageFrame
      title="Proposals"
      description="Every proposal version across trip cases: drafts, sent, approved and superseded."
      actions={
        canWrite ? (
          <Button variant="primary" onClick={() => setPickerOpen(true)}>
            <Plus size={14} /> New proposal
          </Button>
        ) : undefined
      }
    >
      <Section>
        <DataTable<VersionListItem>
          filters={
            <FilterBar
              search={{ placeholder: "Search case code, customer or version title" }}
              selects={[
                {
                  key: "badge",
                  label: "Status",
                  allLabel: "All statuses",
                  options: Object.entries(VERSION_BADGE_META).map(([value, m]) => ({ value, label: m.label })),
                },
                {
                  key: "assignee",
                  label: "Sales",
                  allLabel: "Anyone",
                  options: [
                    { value: "me", label: "Mine" },
                    { value: "none", label: "Unassigned" },
                  ],
                },
              ]}
              dateRange={{ fromKey: "from", toKey: "to", label: "Updated" }}
            />
          }
          searchable={false}
          tableId="proposals"
          enableCsv
          csvFilename="proposals.csv"
          data={list.data?.items ?? []}
          columns={columns}
          getRowId={(r) => r.id}
          loading={list.isLoading}
          error={list.isError ? (list.error as Error).message : null}
          onRetry={() => list.refetch()}
          pageSizeOptions={PAGE_SIZES}
          serverPagination={{ pageIndex: paging.pageIndex, pageSize: paging.pageSize, total: list.data?.total ?? 0, onChange: paging.onChange }}
          emptyTitle="No proposals match"
          emptyDescription="Proposals are created from a trip case. Change the filters to widen the list."
          onRowClick={(r) => router.push(builderHref(r))}
        />
      </Section>

      <ConfirmDialog
        open={action?.kind === "copy"}
        onClose={() => setAction(null)}
        onConfirm={() => target && copy.mutate(target)}
        title="New version"
        message={
          <>
            Create the next version of <strong>{target?.trip_case_code}</strong>. It copies the case&apos;s <strong>latest</strong> version
            (items, title, price) into a new draft, whichever row you started from.
          </>
        }
        confirmLabel="Create version"
        busy={copy.isPending}
        error={copy.error?.message}
      />
      <ConfirmDialog
        open={action?.kind === "delete"}
        onClose={() => setAction(null)}
        onConfirm={() => target && remove.mutate(target)}
        title="Delete draft version"
        message={
          <>
            Delete draft <strong>{target?.trip_case_code} V{target?.version_number}</strong>? It disappears from the case; its number is not
            reused.
          </>
        }
        confirmLabel="Delete draft"
        danger
        busy={remove.isPending}
        error={remove.error?.message}
      />
      {target && (
        <SendProposalModal open={action?.kind === "send"} onClose={() => setAction(null)} code={target.trip_case_code} n={target.version_number} />
      )}
      <CasePicker open={pickerOpen} onClose={() => setPickerOpen(false)} />
    </PageFrame>
  );
}

function RowActions({
  row,
  canWrite,
  onOpen,
  onAction,
}: {
  row: VersionListItem;
  canWrite: boolean;
  onOpen: (href: string) => void;
  onAction: (kind: "copy" | "send" | "delete") => void;
}) {
  const closed = CLOSED.includes(row.trip_case_status);
  const draft = row.badge === "draft";
  const btn =
    "grid h-8 w-8 cursor-pointer place-items-center rounded-md border border-line bg-card text-muted hover:text-ink disabled:cursor-not-allowed disabled:opacity-30";
  const label = `${row.trip_case_code} V${row.version_number}`;
  // Stop the row click so an action never also opens the builder.
  return (
    <div className="flex justify-end gap-1" onClick={(e) => e.stopPropagation()}>
      <button type="button" className={btn} title="Open builder" aria-label={`Open builder for ${label}`} onClick={() => onOpen(builderHref(row))}>
        <Pencil size={14} />
      </button>
      <button type="button" className={btn} title="Preview" aria-label={`Preview ${label}`} onClick={() => onOpen(`${builderHref(row)}/preview`)}>
        <Eye size={14} />
      </button>
      {canWrite && (
        <>
          <button type="button" className={btn} title="New version (copies the latest)" aria-label={`New version of ${row.trip_case_code}`} disabled={closed} onClick={() => onAction("copy")}>
            <CopyPlus size={14} />
          </button>
          <button
            type="button"
            className={btn}
            title={draft ? "Send to customer" : "Only a draft can be sent"}
            aria-label={`Send ${label}`}
            disabled={!draft || !SENDABLE_FROM.includes(row.trip_case_status)}
            onClick={() => onAction("send")}
          >
            <Send size={14} />
          </button>
          <button
            type="button"
            className={`${btn} hover:text-danger`}
            title={draft ? "Delete draft" : "Only a draft can be deleted"}
            aria-label={`Delete ${label}`}
            disabled={!draft || closed}
            onClick={() => onAction("delete")}
          >
            <Trash2 size={14} />
          </button>
        </>
      )}
    </div>
  );
}

/** "+ New proposal": pick a trip case, then open its latest version (or create V1 when it has none). */
function CasePicker({ open, onClose }: { open: boolean; onClose: () => void }) {
  return (
    <Modal open={open} onClose={onClose} title="New proposal" width={560}>
      {open && <CasePickerBody onClose={onClose} />}
    </Modal>
  );
}

function CasePickerBody({ onClose }: { onClose: () => void }) {
  const router = useRouter();
  const qc = useQueryClient();
  const toast = useToast();
  const [q, setQ] = useState("");
  const [debounced, setDebounced] = useState("");
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const t = window.setTimeout(() => setDebounced(q.trim()), 300);
    return () => window.clearTimeout(t);
  }, [q]);
  const cases = useQuery({
    queryKey: ["trip-cases", "picker", debounced],
    queryFn: () => api.tripCases.list({ q: debounced || undefined, sort: "-updated_at", limit: 8 }),
  });

  const choose = async (code: string) => {
    setPending(code);
    setError(null);
    try {
      const detail = await api.tripCases.get(code);
      const latest = detail.versions.reduce<number | null>((m, v) => (m == null || v.version_number > m ? v.version_number : m), null);
      let n = latest;
      if (n == null) {
        const created = await proposalsApi.create(code);
        n = created.version_number;
        await Promise.all([qc.invalidateQueries({ queryKey: ["versions"] }), qc.invalidateQueries({ queryKey: ["trip-case", code] })]);
        toast.success(`${code} V${n} created`);
      }
      onClose();
      router.push(builderHref({ trip_case_code: code, version_number: n }));
    } catch (e) {
      setError((e as Error).message);
      setPending(null);
    }
  };

  return (
    <div className="flex flex-col gap-3 font-sans">
      <p className="text-[13px] text-muted">
        A proposal belongs to a trip case. Pick the case: its latest version opens, or V1 is created from its trip legs when it has none.
      </p>
      <label className="relative block">
        <span className="sr-only">Search trip cases</span>
        <Search size={15} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted2" />
        <input
          autoFocus
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Case code, customer name or e-mail"
          className="h-10 w-full rounded-md border border-line bg-card pl-9 pr-3 text-sm text-ink outline-none focus:border-accent-border"
        />
      </label>
      {cases.isLoading ? (
        <div className="flex items-center gap-2 py-6 text-[13px] text-muted">
          <Spinner /> Loading trip cases…
        </div>
      ) : cases.isError ? (
        <p role="alert" className="text-[13px] text-danger">
          {(cases.error as Error).message}
        </p>
      ) : (cases.data?.items.length ?? 0) === 0 ? (
        <p className="py-6 text-center text-[13px] text-muted">{debounced ? "No trip case matches." : "No trip cases yet. Create one from Trip cases first."}</p>
      ) : (
        <ul className="flex max-h-[360px] flex-col divide-y divide-line2 overflow-y-auto rounded-md border border-line">
          {cases.data!.items.map((c) => {
            const closed = CLOSED.includes(c.status);
            return (
              <li key={c.id}>
                <button
                  type="button"
                  disabled={closed || pending !== null}
                  onClick={() => choose(c.code)}
                  className="flex w-full cursor-pointer items-center justify-between gap-3 px-3 py-2.5 text-left hover:bg-accent-tint disabled:cursor-not-allowed disabled:opacity-50"
                >
                  <span className="min-w-0">
                    <span className="block truncate text-[13px] font-medium text-ink">{c.customer_name ?? "Unnamed customer"}</span>
                    <span className="text-xs text-muted">
                      <Code>{c.code}</Code>
                      {c.start_date ? ` · ${formatDate(c.start_date)}` : ""}
                    </span>
                  </span>
                  <span className="flex shrink-0 items-center gap-2">
                    {pending === c.code && <Spinner />}
                    <TripCaseStatusBadge status={c.status} />
                  </span>
                </button>
              </li>
            );
          })}
        </ul>
      )}
      {error && (
        <p role="alert" className="text-[13px] text-danger">
          {error}
        </p>
      )}
    </div>
  );
}

/** A flexible table cell: at least `min` px wide on desktop, wider when the fixed columns leave room. Its lines use
 *  CLAMP (one line, ellipsis) instead of `truncate`: a clamped line can wrap, so its min-content width is one word and
 *  the long text never sets the column width. Phone cards (kit DataTable < 768 px = Tailwind below `md`) show the full
 *  text. */
function FlexCell({ min, children }: { min: number; children: React.ReactNode }) {
  return (
    <span className="block md:[min-width:var(--flex-min)]" style={{ "--flex-min": `${min}px` } as React.CSSProperties}>
      {children}
    </span>
  );
}

const CLAMP = "block break-words md:line-clamp-1";

const COLUMNS: ColumnDef<VersionListItem, unknown>[] = [
  {
    id: "proposal",
    header: "Proposal",
    accessorFn: (r) => `${r.trip_case_code} V${r.version_number} ${r.title ?? r.trip_title ?? "Custom trip"}`,
    enableSorting: false,
    cell: ({ row }) => {
      const title = row.original.title ?? row.original.trip_title ?? "Custom trip";
      const customer = row.original.customer.name ?? "Unnamed customer";
      return (
        <FlexCell min={200}>
          <span title={title} className={`${CLAMP} text-[13px] font-medium text-ink`}>
            <span className="font-semibold text-accent-deep">V{row.original.version_number}</span> {title}
          </span>
          <span title={customer} className={`${CLAMP} text-xs text-muted`}>
            <Code>{row.original.trip_case_code}</Code> · {customer}
          </span>
        </FlexCell>
      );
    },
  },
  {
    id: "legs",
    header: "Trip",
    accessorFn: (r) => r.leg_titles.join(" + "),
    enableSorting: false,
    cell: ({ row }) => {
      const { leg_titles: legs, start_date, duration_days } = row.original;
      const when = [start_date ? formatDate(start_date) : null, duration_days ? `${duration_days} days` : null].filter(Boolean).join(" · ");
      return (
        <FlexCell min={160}>
          <span title={legs.length ? legs.join("\n") : undefined} className="flex min-w-0 text-[13px] text-body">
            <span className="break-words md:line-clamp-1">{legs[0] ?? "Custom"}</span>
            {legs.length > 1 && <span className="shrink-0 pl-1 text-muted">+{legs.length - 1}</span>}
          </span>
          <span className={`${CLAMP} text-xs text-muted`}>{when || "No dates yet"}</span>
        </FlexCell>
      );
    },
  },
  { id: "badge", header: "Status", accessorKey: "badge", cell: ({ row }) => <VersionBadgePill badge={row.original.badge} /> },
  {
    id: "total",
    header: "Total",
    accessorFn: (r) => csvMoney(r.total_price, r.currency),
    enableSorting: false,
    cell: ({ row }) => <Money amount={row.original.total_price} currency={row.original.currency} />,
  },
  {
    id: "edited",
    header: "Last edited",
    accessorFn: (r) => r.updated_at,
    enableSorting: false,
    cell: ({ row }) => (
      <span className="text-[13px] text-muted">
        <DateText value={row.original.updated_at} relative />
        {row.original.updated_by?.name && <span className="block text-xs">by {row.original.updated_by.name}</span>}
      </span>
    ),
  },
];
