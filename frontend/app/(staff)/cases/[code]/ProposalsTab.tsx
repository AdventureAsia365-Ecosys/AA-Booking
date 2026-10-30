"use client";
// app/(staff)/cases/[code]/ProposalsTab.tsx
// AA-683 — the Proposals tab of the case workspace (spec §4.7): the case's versions, read from the case detail.
// AA-996 (Figma TRIP CASES DETAILS-1, audit item 7): each row has its actions, like the /proposals list: Preview,
// Send to customer (the shared send modal; an unsent draft while the case is in discovery, building or proposal sent)
// and Discard draft (DELETE …/versions/{n}: Booking never hard-deletes a sent or approved version, so Figma's Delete
// is discarding any unsent, unapproved version, the API's rule, which includes an older draft badged "superseded").
// Send and Discard need version.write; a closed case keeps its versions as they are.
// A row opens the builder.

import { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Eye, Send, Trash2 } from "lucide-react";
import { proposalsApi } from "@/lib/api/proposals";
import type { CaseVersion, TripCaseDetail } from "@/lib/api/types";
import {
  ConfirmDialog, DataTable, DateText, IconButton, Money, Section, VersionBadgePill, csvMoney, useCan, useToast,
} from "@/ui";
import { SendProposalModal } from "@/app/(staff)/proposals/_parts/SendProposalModal";

const CLOSED = ["completed", "cancelled"];
const SENDABLE_FROM = ["discovery", "building", "proposal_sent"];

type RowAction = { kind: "send" | "discard"; n: number } | null;

export function ProposalsTab({ c }: { c: TripCaseDetail }) {
  const router = useRouter();
  const qc = useQueryClient();
  const toast = useToast();
  const canWrite = useCan("version.write");
  const [action, setAction] = useState<RowAction>(null);
  const builderHref = (n: number) => `/cases/${encodeURIComponent(c.code)}/proposals/${n}`;
  const closed = CLOSED.includes(c.status);
  const sendable = SENDABLE_FROM.includes(c.status);

  const discard = useMutation({
    mutationFn: (n: number) => proposalsApi.remove(c.code, n),
    onSuccess: async (_, n) => {
      await Promise.all([qc.invalidateQueries({ queryKey: ["trip-case", c.code] }), qc.invalidateQueries({ queryKey: ["versions"] })]);
      toast.success(`Draft V${n} discarded`);
      setAction(null);
    },
  });

  const columns: ColumnDef<CaseVersion, unknown>[] = [
    {
      id: "v",
      header: "Version",
      accessorKey: "version_number",
      cell: ({ row }) => (
        <Link
          href={builderHref(row.original.version_number)}
          onClick={(e) => e.stopPropagation()}
          className="font-semibold text-accent-deep hover:underline"
        >
          V{row.original.version_number}
        </Link>
      ),
    },
    { id: "title", header: "Title / notes", accessorFn: (r) => r.title ?? r.notes ?? "", enableSorting: false, cell: ({ row }) => <span className="text-[13px] text-body">{row.original.title ?? row.original.notes ?? "—"}</span> },
    { id: "badge", header: "Status", accessorKey: "badge", cell: ({ row }) => <VersionBadgePill badge={row.original.badge} /> },
    { id: "items", header: "Items", accessorKey: "item_count", cell: ({ row }) => <span className="bk-tabular text-[13px]">{row.original.item_count}</span> },
    { id: "total", header: "Total", accessorFn: (r) => csvMoney(r.total_price, r.currency), enableSorting: false, cell: ({ row }) => <Money amount={row.original.total_price} currency={row.original.currency} /> },
    { id: "sent", header: "Sent", accessorFn: (r) => r.sent_at ?? "", enableSorting: false, cell: ({ row }) => <DateText value={row.original.sent_at} className="whitespace-nowrap text-[13px] text-muted" /> },
    {
      id: "updated",
      header: "Updated",
      accessorFn: (r) => r.updated_at,
      enableSorting: false,
      cell: ({ row }) => <DateText value={row.original.updated_at} relative className="whitespace-nowrap text-[13px] text-muted" />,
    },
    // Row tools: the Booking DataTable leaves the "actions" column out of the CSV (NON_DATA_IDS).
    {
      id: "actions",
      header: () => <span className="sr-only">Actions</span>,
      enableSorting: false,
      cell: ({ row }) => {
        const v = row.original;
        const draft = v.badge === "draft";
        // Discard follows the API's own rule (versions._editable): any unsent, unapproved version, including an
        // older draft shown as "superseded" once a later version went out.
        const discardable = !v.is_sent_to_customer && !v.is_approved;
        const label = `${c.code} V${v.version_number}`;
        const sendWhy = !draft
          ? "Only a draft can be sent"
          : !sendable
            ? `A proposal can be sent while the case is in discovery, building or proposal sent`
            : v.item_count === 0
              ? "Add at least one item before sending"
              : v.total_price == null
                ? "Apply a price in the builder before sending"
                : "Send to customer";
        const canSendRow = draft && sendable && v.item_count > 0 && v.total_price != null;
        // Stop the row click so an action never also opens the builder.
        return (
          <div className="flex justify-end gap-1" onClick={(e) => e.stopPropagation()} data-testid="version-actions">
            <IconButton title="Preview" label={`Preview ${label}`} onClick={() => router.push(`${builderHref(v.version_number)}/preview`)}>
              <Eye size={14} />
            </IconButton>
            {canWrite && (
              <>
                <IconButton
                  title={sendWhy}
                  label={`Send ${label} to the customer`}
                  disabled={!canSendRow}
                  onClick={() => setAction({ kind: "send", n: v.version_number })}
                >
                  <Send size={14} />
                </IconButton>
                <IconButton
                  tone="danger"
                  title={!discardable ? "A sent or approved version is kept" : closed ? "The case is closed" : "Discard draft"}
                  label={`Discard draft ${label}`}
                  disabled={!discardable || closed}
                  onClick={() => {
                    discard.reset();
                    setAction({ kind: "discard", n: v.version_number });
                  }}
                >
                  <Trash2 size={14} />
                </IconButton>
              </>
            )}
          </div>
        );
      },
    },
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
        onRowClick={(r) => router.push(builderHref(r.version_number))}
        emptyTitle="No proposal yet"
        emptyDescription="The first version is built from the trip legs."
        tableMinWidth={720}
      />
      <ConfirmDialog
        open={action?.kind === "discard"}
        onClose={() => setAction(null)}
        onConfirm={() => action && discard.mutate(action.n)}
        title="Discard draft version"
        message={
          <>
            Discard draft <strong>{c.code} V{action?.n}</strong>? It disappears from the case; its number is not reused. Sent and approved
            versions are kept.
          </>
        }
        confirmLabel="Discard draft"
        danger
        busy={discard.isPending}
        error={discard.error?.message}
      />
      {action?.kind === "send" && <SendProposalModal open onClose={() => setAction(null)} code={c.code} n={action.n} />}
    </Section>
  );
}
