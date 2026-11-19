"use client";
// app/(staff)/proposals/page.tsx — Cross-case proposal versions (W1, UI spec §4.11; Figma ITINERARY BUILDER,
// -1). GET /booking/versions with server pagination; filters in the URL.
// Row actions: Open builder, Preview, New version (POST …/versions: copies the case's LATEST version, the
// only copy the API offers), Delete draft (DELETE …/versions/{n}). Send to the customer is retired (AA-1046).
// "+ New proposal": a case picker (GET /booking/trip-cases?q=) that opens the case's latest version, or
// creates V1 when the case has none.
// Layout (AA-683 S239): the table fits a 1440 px screen without sideways scroll. Proposal (V-number + title, case
// code + customer) and Trip (first leg, dates + length) are the flexible columns, one truncated line per row with the
// full text in the tooltip; the fixed columns keep their natural width. Version and Dates are no longer columns of
// their own: they sit in those two cells.
// AA-1037 (root ADR 0019 d8): while the B2C part is closed a row on a b2c case (trip_case_channel) offers no write
// (New version, Delete draft: the API answers 410 b2b_only) and the case picker does not offer b2c cases.

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { CopyPlus, Eye, Pencil, Plus, Search, Trash2 } from "lucide-react";
import { api } from "@/lib/api/client";
import { B2C_INTAKE_OPEN } from "@/lib/config";
import { proposalsApi } from "@/lib/api/proposals";
import type { VersionListItem } from "@/lib/api/types";
import { PAGE_SIZES, useOffsetPage } from "@/lib/useOffsetPage";
import {
  Input,
  Button,
  Code,
  ConfirmDialog,
  DataTable,
  DateText,
  FilterBar,
  Modal,
  Money,
  PageFrame,
  IconButton,
  ListButton,
  Section,
  Spinner,
  TripCaseStatusBadge,
  VERSION_BADGE_META,
  VersionBadgePill,
  csvMoney,
  useCan,
  useToast,
  useUrlFilters,
} from "@/ui";
import { B2C_READ_ONLY_NOTE, isFrozenCase } from "@/ui/booking/b2cFrozen";
import { casePartyLabel } from "@/ui/booking/caseFacts";
import { useT } from "@/ui/core/useI18n";
import { useTx } from "@/ui/local/useTx";
import { MSG } from "./page.messages";
import { useBookingFormat } from "@/ui/local/useBookingFormat";
import { daysText } from "@/ui/booking/plural";

const CLOSED = ["completed", "cancelled"];
const builderHref = (r: { trip_case_code: string; version_number: number }) =>
  `/cases/${encodeURIComponent(r.trip_case_code)}/proposals/${r.version_number}`;

type RowAction = { kind: "copy" | "delete"; row: VersionListItem } | null;

export default function ProposalsPage() {
  const tr = useT(MSG);
  const tx = useTx();
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
      toast.success(tr("{0} V{1} created", r.trip_case_code, v.version_number));
      setAction(null);
      router.push(builderHref({ trip_case_code: r.trip_case_code, version_number: v.version_number }));
    },
  });
  const remove = useMutation({
    mutationFn: (r: VersionListItem) => proposalsApi.remove(r.trip_case_code, r.version_number),
    onSuccess: async (_, r) => {
      await Promise.all([qc.invalidateQueries({ queryKey: ["versions"] }), qc.invalidateQueries({ queryKey: ["trip-case", r.trip_case_code] })]);
      toast.success(tr("Draft {0} V{1} deleted", r.trip_case_code, r.version_number));
      setAction(null);
    },
  });

  const columns = useMemo<ColumnDef<VersionListItem, unknown>[]>(
    () => [
      ...COLUMNS,
      {
        id: "actions",
        header: () => <span className="sr-only">{tr("Actions")}</span>,
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
    [canWrite, router, tr],
  );

  const target = action?.row;
  return (
    <PageFrame
      title={tr("Proposals")}
      description={tr("Every proposal version across trip cases: drafts, sent, approved and superseded.")}
      actions={
        canWrite ? (
          <Button variant="primary" onClick={() => setPickerOpen(true)}>
            <Plus size={14} /> {tr("New proposal")}
          </Button>
        ) : undefined
      }
    >
      <Section>
        <DataTable<VersionListItem>
          filters={
            <FilterBar
              search={{ placeholder: tr("Search case code, customer or version title") }}
              selects={[
                {
                  key: "badge",
                  label: tr("Status"),
                  allLabel: tr("All statuses"),
                  options: Object.entries(VERSION_BADGE_META).map(([value, m]) => ({ value, label: tx(m.label) })),
                },
                {
                  key: "assignee",
                  label: tr("Sales"),
                  allLabel: tr("Anyone"),
                  options: [
                    { value: "me", label: tr("Mine") },
                    { value: "none", label: tr("Unassigned") },
                  ],
                },
              ]}
              dateRange={{ fromKey: "from", toKey: "to", label: tr("Updated") }}
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
          emptyTitle={tr("No proposals match")}
          emptyDescription={tr("Proposals are created from a trip case. Change the filters to widen the list.")}
          onRowClick={(r) => router.push(builderHref(r))}
        />
      </Section>

      <ConfirmDialog
        open={action?.kind === "copy"}
        onClose={() => setAction(null)}
        onConfirm={() => target && copy.mutate(target)}
        title={tr("New version")}
        message={
          <>
            {tr("Create the next version of")} <strong>{target?.trip_case_code}</strong>{tr(". It copies the case's")} <strong>{tr("latest")}</strong> {tr("version (items, title, price) into a new draft, whichever row you started from.")}
          </>
        }
        confirmLabel={tr("Create version")}
        busy={copy.isPending}
        error={tx(copy.error?.message)}
      />
      <ConfirmDialog
        open={action?.kind === "delete"}
        onClose={() => setAction(null)}
        onConfirm={() => target && remove.mutate(target)}
        title={tr("Delete draft version")}
        message={
          <>
            {tr("Delete draft")} <strong>{target?.trip_case_code} V{target?.version_number}</strong>{tr("? It disappears from the case; its number is not reused.")}
          </>
        }
        confirmLabel={tr("Delete draft")}
        danger
        busy={remove.isPending}
        error={tx(remove.error?.message)}
      />
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
  onAction: (kind: "copy" | "delete") => void;
}) {
  const tr = useT(MSG);
  const closed = CLOSED.includes(row.trip_case_status);
  const draft = row.badge === "draft";
  const label = `${row.trip_case_code} V${row.version_number}`;
  const writable = canWrite && !isFrozenCase(row.trip_case_channel, B2C_INTAKE_OPEN);
  // Stop the row click so an action never also opens the builder.
  return (
    <div className="flex justify-end gap-1" onClick={(e) => e.stopPropagation()}>
      <IconButton title={tr("Open builder")} label={tr("Open builder for {0}", label)} onClick={() => onOpen(builderHref(row))}>
        <Pencil size={14} />
      </IconButton>
      <IconButton title={tr("Preview")} label={tr("Preview {0}", label)} onClick={() => onOpen(`${builderHref(row)}/preview`)}>
        <Eye size={14} />
      </IconButton>
      {writable && (
        <>
          <IconButton
            title={tr("New version (copies the latest)")}
            label={tr("New version of {0}", row.trip_case_code)}
            disabled={closed}
            onClick={() => onAction("copy")}
          >
            <CopyPlus size={14} />
          </IconButton>
          <IconButton
            tone="danger"
            title={draft ? tr("Delete draft") : tr("Only a draft can be deleted")}
            label={tr("Delete {0}", label)}
            disabled={!draft || closed}
            onClick={() => onAction("delete")}
          >
            <Trash2 size={14} />
          </IconButton>
        </>
      )}
    </div>
  );
}

/** "+ New proposal": pick a trip case, then open its latest version (or create V1 when it has none). */
function CasePicker({ open, onClose }: { open: boolean; onClose: () => void }) {
  const tr = useT(MSG);
  return (
    <Modal open={open} onClose={onClose} title={tr("New proposal")} width={560}>
      {open && <CasePickerBody onClose={onClose} />}
    </Modal>
  );
}

function CasePickerBody({ onClose }: { onClose: () => void }) {
  const tr = useT(MSG);
  const { formatDay } = useBookingFormat();
  const tx = useTx();
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
        toast.success(tr("{0} V{1} created", code, n));
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
        {tr("A proposal belongs to a trip case. Pick the case: its latest version opens, or V1 is created from its trip legs when it has none.")}
      </p>
      <label className="relative block">
        <span className="sr-only">{tr("Search trip cases")}</span>
        <Search size={15} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted2" />
        <Input
          autoFocus
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder={tr("Case code, customer name or e-mail")}
          className="w-full" style={{ paddingLeft: 36 }}
        />
      </label>
      {cases.isLoading ? (
        <div className="flex items-center gap-2 py-6 text-[13px] text-muted">
          <Spinner /> {tr("Loading trip cases…")}
        </div>
      ) : cases.isError ? (
        <p role="alert" className="text-[13px] text-danger">
          {tx((cases.error as Error).message)}
        </p>
      ) : (cases.data?.items.length ?? 0) === 0 ? (
        <p className="py-6 text-center text-[13px] text-muted">{debounced ? tr("No trip case matches.") : tr("No trip cases yet. Create one from Trip cases first.")}</p>
      ) : (
        <ul className="flex max-h-[360px] flex-col divide-y divide-line2 overflow-y-auto rounded-md border border-line">
          {cases.data!.items.map((c) => {
            const closed = CLOSED.includes(c.status);
            const frozen = isFrozenCase(c.channel, B2C_INTAKE_OPEN);
            return (
              <li key={c.id}>
                <ListButton
                  disabled={closed || frozen || pending !== null}
                  title={frozen ? B2C_READ_ONLY_NOTE : undefined}
                  onClick={() => choose(c.code)}
                  className="justify-between"
                  style={{ gap: 12, padding: "10px 12px", borderRadius: 0 }}
                >
                  <span className="min-w-0">
                    <span className="block truncate text-[13px] font-medium text-ink">{tx(casePartyLabel(c))}</span>
                    <span className="text-xs text-muted">
                      <Code>{c.code}</Code>
                      {c.start_date ? ` · ${formatDay(c.start_date)}` : ""}
                    </span>
                  </span>
                  <span className="flex shrink-0 items-center gap-2">
                    {pending === c.code && <Spinner />}
                    {frozen && <span className="text-xs text-muted">{tr("Read-only")}</span>}
                    <TripCaseStatusBadge status={c.status} />
                  </span>
                </ListButton>
              </li>
            );
          })}
        </ul>
      )}
      {error && (
        <p role="alert" className="text-[13px] text-danger">
          {tx(error)}
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

/** "by <name>" under the last-edited date, in the UI language (the column is defined outside a component). */
function UpdatedBy({ name }: { name: string }) {
  const t = useT(MSG);
  return <span className="block text-xs">{t("by {0}", name)}</span>;
}

/** The trip's start day and length ("9 Oct 2026 · 7 days") in the UI language, for the module-level columns. */
function TripWhen({ start, days }: { start: string | null; days: number | null }) {
  const t = useT(MSG);
  const tx = useTx();
  const { formatDay } = useBookingFormat();
  const when = [start ? formatDay(start) : null, days ? tx(daysText(days)) : null].filter(Boolean).join(" · ");
  return <span className={`${CLAMP} text-xs text-muted`}>{when || t("No dates yet")}</span>;
}

const COLUMNS: ColumnDef<VersionListItem, unknown>[] = [
  {
    id: "proposal",
    header: "Proposal",
    accessorFn: (r) => `${r.trip_case_code} V${r.version_number} ${r.title ?? r.trip_title ?? "Custom trip"}`,
    enableSorting: false,
    cell: ({ row }) => {
      const title = row.original.title ?? row.original.trip_title ?? "Custom trip";
      const customer = row.original.trip_case_channel === "b2b" ? "B2B agency" : "Traveller (B2C history)";
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
      return (
        <FlexCell min={160}>
          <span title={legs.length ? legs.join("\n") : undefined} className="flex min-w-0 text-[13px] text-body">
            <span className="break-words md:line-clamp-1">{legs[0] ?? "Custom"}</span>
            {legs.length > 1 && <span className="shrink-0 pl-1 text-muted">+{legs.length - 1}</span>}
          </span>
          <TripWhen start={start_date} days={duration_days} />
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
        {row.original.updated_by?.name && <UpdatedBy name={row.original.updated_by.name} />}
      </span>
    ),
  },
];
