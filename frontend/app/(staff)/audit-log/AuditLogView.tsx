"use client";
// app/(staff)/audit-log/AuditLogView.tsx — the audit log list (admin; UI spec §3 `/audit-log`, §5 AuditTimeline).
//
// Data: GET /booking/audit-logs (live since bk-005, permission audit.read = admin). Filters live in the URL and map
// 1:1 to the API: entity_type, actor (CIS user id), from / to (dates, `to` inclusive) and entity_id (set by "Only
// this record" on a row, or by a link such as /audit-log?entity_id=<uuid>). Keyset pages via `before`
// ("Show older"); a keyset page carries no total, so the footer counts loaded rows.
// Not built (no API yet): free-text search and the server CSV export (`?format=csv`, spec §6.3). "Export CSV"
// downloads the rows loaded on screen.

import { useState } from "react";
import Link from "next/link";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { ClipboardList, Download, Lock, X } from "lucide-react";
import { ApiError, api } from "@/lib/api/client";
import type { AuditLogItem } from "@/lib/api/types";
import {
  AUDIT_ENTITY_LABEL,
  Button,
  Code,
  DateText,
  EmptyState,
  ErrorState,
  FilterBar,
  PageFrame,
  Pill,
  Section,
  SkeletonBar,
  auditActionLabel,
  auditActionTone,
  auditChanges,
  auditEntityHref,
  auditEntityLabel,
  downloadCsv,
  toCsv,
  useCan,
  useUrlFilters,
} from "@/ui";
import { retryUnlessClientError } from "@/ui/booking/QueryProvider";

const PAGE_SIZE = 50;
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

export function AuditLogView() {
  const canRead = useCan("audit.read");
  const { get, set } = useUrlFilters();
  const entityId = get("entity_id");
  const entityIdValid = UUID_RE.test(entityId);
  const filters = {
    entity_type: get("entity_type") || undefined,
    actor: get("actor") || undefined,
    from: DATE_RE.test(get("from")) ? get("from") : undefined,
    to: DATE_RE.test(get("to")) ? get("to") : undefined,
    entity_id: entityIdValid ? entityId : undefined,
    limit: PAGE_SIZE,
  };
  const filtered = Object.entries(filters).some(([k, v]) => k !== "limit" && v !== undefined);

  const feed = useInfiniteQuery({
    queryKey: ["audit", "log", filters],
    queryFn: ({ pageParam }) => api.reference.auditLogs({ ...filters, before: pageParam }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_before ?? undefined,
    enabled: canRead,
    retry: retryUnlessClientError,
  });
  const staff = useQuery({
    queryKey: ["staff", "list", { active: undefined }],
    queryFn: () => api.staff.list(),
    enabled: canRead,
    retry: retryUnlessClientError,
    staleTime: 5 * 60_000,
  });

  const items = feed.data?.pages.flatMap((p) => p.items) ?? [];
  const forbidden = !canRead || (feed.error instanceof ApiError && feed.error.status === 403);
  const staffOptions = (staff.data?.items ?? [])
    .map((s) => ({ value: s.cis_user_id, label: s.name ?? s.email ?? s.cis_user_id }))
    .sort((a, b) => a.label.localeCompare(b.label));
  // The label of the filtered record, from any loaded row of it (the API filters by id only).
  const entityLabel = entityIdValid ? items.find((r) => r.entity_id === entityId)?.entity_label : undefined;

  return (
    <PageFrame
      title="Audit log"
      description="Every staff write in Booking, newest first: who did what, when, and the values before and after."
      actions={
        <Button variant="secondary" onClick={() => exportCsv(items)} disabled={items.length === 0}>
          <Download size={15} /> Export CSV
        </Button>
      }
    >
      <Section padded={false}>
        <div className="border-b border-line2 px-4 py-4 sm:px-5">
          <FilterBar
            selects={[
              {
                key: "entity_type",
                label: "Record type",
                allLabel: "All record types",
                options: Object.entries(AUDIT_ENTITY_LABEL).map(([value, label]) => ({ value, label })),
              },
              { key: "actor", label: "Staff member", allLabel: "All staff", options: staffOptions },
            ]}
            dateRange={{ fromKey: "from", toKey: "to", label: "Date" }}
            extraKeys={["entity_id"]}
          >
            {entityId && (
              <span className="inline-flex h-9 max-w-full items-center gap-1.5 rounded-full border border-accent-border bg-accent-tint px-3 text-[13px] text-accent-deep">
                <span className="truncate">
                  {entityIdValid ? <>Record: {entityLabel ?? `${entityId.slice(0, 8)}…`}</> : "Record id not valid (ignored)"}
                </span>
                <button
                  type="button"
                  onClick={() => set("entity_id", null)}
                  aria-label="Show every record"
                  className="cursor-pointer rounded-full p-0.5 hover:bg-card"
                >
                  <X size={13} />
                </button>
              </span>
            )}
          </FilterBar>
        </div>
        {forbidden ? (
          <EmptyState
            icon={<Lock size={28} />}
            title="You do not have access to the audit log"
            description="Only Booking admins can read the audit log."
          />
        ) : feed.isLoading ? (
          <ListSkeleton />
        ) : feed.isError ? (
          <ErrorState message={(feed.error as Error).message} onRetry={() => feed.refetch()} />
        ) : items.length === 0 ? (
          filtered ? (
            <EmptyState title="No entries match these filters" description="Change or reset the filters to see more." />
          ) : (
            <EmptyState
              icon={<ClipboardList size={28} />}
              title="No audited actions yet"
              description="Every change staff make in Booking (cases, proposals, customers, staff) is recorded here."
            />
          )
        ) : (
          <ul aria-label="Audit log entries">
            {items.map((row) => (
              <AuditRow key={row.id} row={row} filteredByEntity={entityIdValid && row.entity_id === entityId} onOnly={(id) => set("entity_id", id)} />
            ))}
          </ul>
        )}
        {!forbidden && items.length > 0 && (
          <div className="flex flex-wrap items-center justify-between gap-3 border-t border-line2 px-4 py-3 text-[13px] text-muted sm:px-5">
            <span>
              Showing {items.length} entr{items.length === 1 ? "y" : "ies"}
              {feed.hasNextPage ? ", newest first" : ""}
            </span>
            {feed.hasNextPage && (
              <Button variant="secondary" size="sm" onClick={() => feed.fetchNextPage()} disabled={feed.isFetchingNextPage}>
                {feed.isFetchingNextPage ? "Loading…" : "Show older"}
              </Button>
            )}
          </div>
        )}
      </Section>
    </PageFrame>
  );
}

function actorText(row: AuditLogItem): string {
  if (row.actor.name) return row.actor.name;
  if (row.actor.type === "system") return "System";
  return row.actor.id ?? "Unknown";
}

function AuditRow({
  row,
  filteredByEntity,
  onOnly,
}: {
  row: AuditLogItem;
  filteredByEntity: boolean;
  onOnly: (entityId: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const changes = auditChanges(row.before, row.after);
  const href = auditEntityHref(row);
  const label = row.entity_label;
  return (
    <li className="border-b border-line2 px-4 py-3 last:border-b-0 sm:px-5" data-testid="audit-row">
      <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-1">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <Pill tone={auditActionTone(row.action)} dot={false} upper>
            {auditActionLabel(row.action)}
          </Pill>
          <span className="text-[12px] text-muted">{auditEntityLabel(row.entity_type)}</span>
          {label &&
            (href ? (
              <Link href={href} className="min-w-0 hover:underline">
                <Code>{label}</Code>
              </Link>
            ) : (
              <Code>{label}</Code>
            ))}
        </div>
        <DateText value={row.at} withTime className="shrink-0 text-xs text-muted" />
      </div>
      <p className="mt-1 break-words text-[13px] text-ink">{row.summary ?? row.action}</p>
      <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted">
        <span>
          By{" "}
          {row.actor.type === "staff" && row.actor.id ? (
            <Link href={`/staff/${encodeURIComponent(row.actor.id)}`} className="font-medium text-body hover:underline">
              {actorText(row)}
            </Link>
          ) : (
            <span className="font-medium text-body">{actorText(row)}</span>
          )}
        </span>
        {row.entity_id && !filteredByEntity && (
          <button
            type="button"
            onClick={() => onOnly(row.entity_id as string)}
            className="cursor-pointer font-semibold text-accent-deep hover:underline"
          >
            Only this record
          </button>
        )}
        {changes.length > 0 && (
          <button
            type="button"
            onClick={() => setOpen((o) => !o)}
            aria-expanded={open}
            className="cursor-pointer font-semibold text-accent-deep hover:underline"
          >
            {open ? "Hide changes" : `Show changes (${changes.length})`}
          </button>
        )}
      </div>
      {open && (
        // A small before / after grid (not the kit DataTable: no sorting or paging for a handful of fields).
        <div role="table" aria-label="Changes" className="mt-2 rounded-md bg-panel text-xs">
          <div role="row" className="grid grid-cols-[minmax(0,28%)_minmax(0,1fr)_minmax(0,1fr)] text-[11px] uppercase tracking-[0.04em] text-muted">
            <span role="columnheader" className="px-2 py-1.5 font-semibold">Field</span>
            <span role="columnheader" className="px-2 py-1.5 font-semibold">Before</span>
            <span role="columnheader" className="px-2 py-1.5 font-semibold">After</span>
          </div>
          {changes.map((c) => (
            <div
              role="row"
              key={c.field}
              className="grid grid-cols-[minmax(0,28%)_minmax(0,1fr)_minmax(0,1fr)] border-t border-line2"
            >
              <span role="cell" className="break-words px-2 py-1.5 font-mono text-body">{c.field}</span>
              <span role="cell" className="break-all px-2 py-1.5 text-muted">{c.before || "—"}</span>
              <span role="cell" className={`break-all px-2 py-1.5 ${c.before !== c.after ? "font-medium text-ink" : "text-muted"}`}>
                {c.after || "—"}
              </span>
            </div>
          ))}
        </div>
      )}
    </li>
  );
}

function exportCsv(rows: AuditLogItem[]) {
  const csv = toCsv(rows, [
    { header: "At", value: (r) => r.at },
    { header: "Actor", value: (r) => actorText(r) },
    { header: "Actor id", value: (r) => r.actor.id },
    { header: "Action", value: (r) => r.action },
    { header: "Record type", value: (r) => r.entity_type },
    { header: "Record", value: (r) => r.entity_label },
    { header: "Record id", value: (r) => r.entity_id },
    { header: "Summary", value: (r) => r.summary },
    { header: "Before", value: (r) => (r.before == null ? "" : JSON.stringify(r.before)) },
    { header: "After", value: (r) => (r.after == null ? "" : JSON.stringify(r.after)) },
  ]);
  downloadCsv(`booking-audit-log-${new Date().toISOString().slice(0, 10)}.csv`, csv);
}

function ListSkeleton() {
  return (
    <ul aria-busy="true" aria-label="Loading the audit log">
      {[0, 1, 2, 3, 4].map((i) => (
        <li key={i} className="flex flex-col gap-2 border-b border-line2 px-5 py-4 last:border-b-0">
          <SkeletonBar w="35%" h={12} />
          <SkeletonBar w="80%" h={12} />
        </li>
      ))}
    </ul>
  );
}
