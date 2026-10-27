"use client";
// app/(staff)/cases/[code]/ProposalsTab.tsx
// AA-683 — the Proposals tab of the case workspace (spec §4.7): the case's versions, read from the case
// detail. Moved out of page.tsx unchanged so the builder lane can own its actions (new version, duplicate,
// delete draft) without touching the workspace page.

import Link from "next/link";
import type { ColumnDef } from "@tanstack/react-table";
import type { CaseVersion, TripCaseDetail } from "@/lib/api/types";
import { DataTable, DateText, Money, Section, VersionBadgePill, csvMoney } from "@/ui";

export function ProposalsTab({ c }: { c: TripCaseDetail }) {
  const columns: ColumnDef<CaseVersion, unknown>[] = [
    {
      id: "v",
      header: "Version",
      accessorKey: "version_number",
      cell: ({ row }) => (
        <Link href={`/cases/${encodeURIComponent(c.code)}/proposals/${row.original.version_number}`} className="font-semibold text-accent-deep hover:underline">
          V{row.original.version_number}
        </Link>
      ),
    },
    { id: "title", header: "Title / notes", accessorFn: (r) => r.title ?? r.notes ?? "", enableSorting: false, cell: ({ row }) => <span className="text-[13px] text-body">{row.original.title ?? row.original.notes ?? "—"}</span> },
    { id: "badge", header: "Status", accessorKey: "badge", cell: ({ row }) => <VersionBadgePill badge={row.original.badge} /> },
    { id: "items", header: "Items", accessorKey: "item_count", cell: ({ row }) => <span className="bk-tabular text-[13px]">{row.original.item_count}</span> },
    { id: "total", header: "Total", accessorFn: (r) => csvMoney(r.total_price, r.currency), enableSorting: false, cell: ({ row }) => <Money amount={row.original.total_price} currency={row.original.currency} /> },
    { id: "sent", header: "Sent", accessorFn: (r) => r.sent_at ?? "", enableSorting: false, cell: ({ row }) => <DateText value={row.original.sent_at} className="text-[13px] text-muted" /> },
    { id: "updated", header: "Updated", accessorFn: (r) => r.updated_at, enableSorting: false, cell: ({ row }) => <DateText value={row.original.updated_at} relative className="text-[13px] text-muted" /> },
  ];
  return (
    <Section title="Proposal versions" description="Each version is a full itinerary and price. A sent version is locked.">
      <DataTable<CaseVersion>
        tableId="case-versions"
        enableCsv
        csvFilename={`${c.code}-proposals.csv`}
        data={c.versions}
        columns={columns}
        getRowId={(r) => r.id}
        emptyTitle="No proposal yet"
        emptyDescription="The first version is built from the trip legs."
        tableMinWidth={720}
      />
    </Section>
  );
}
