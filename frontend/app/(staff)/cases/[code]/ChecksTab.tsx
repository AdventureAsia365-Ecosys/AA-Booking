"use client";
// app/(staff)/cases/[code]/ChecksTab.tsx
// AA-682 S2 (bk-027, S252 design §6) — the Checks tab of the case workspace: the derived checks (every supplier booking
// confirmed, from the Suppliers tab; payment verified, from invoices in a later release) and the stored checks (three
// standard ones added when the trip was confirmed, plus custom ones). A row opens the check drawer; "Add check" adds a
// custom one. GET /booking/trip-cases/{ref}/checks (ops.read); writes need ops.write and an open trip.

import { useState } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { CheckCircle2, Circle, Clock, Plus } from "lucide-react";
import { ApiError } from "@/lib/api/client";
import { tripChecksApi, type DerivedCheck, type TripCheck } from "@/lib/api/tripChecks";
import type { TripCaseDetail } from "@/lib/api/types";
import { Button, DataTable, DateText, Pill, Section, TripCheckStatusBadge, useCan } from "@/ui";
import { CheckDrawer, NewCheckDrawer, checkTitle, isOverdue } from "../../trip-checks/_parts/CheckDrawer";

const SOLD = ["confirmed", "supplier_confirming", "invoice_sent", "payment_pending", "paid", "pre_departure", "on_trip", "completed"];

export function ChecksTab({ c }: { c: TripCaseDetail }) {
  const canWrite = useCan("ops.write");
  const q = useQuery({ queryKey: ["trip-case", c.code, "checks"], queryFn: () => tripChecksApi.forCase(c.code) });
  const [openId, setOpenId] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const data = q.data;
  const open = data?.items.find((i) => i.id === openId) ?? null;
  const sold = SOLD.includes(c.status);

  const columns: ColumnDef<TripCheck, unknown>[] = [
    {
      id: "task",
      header: "Check",
      cell: ({ row }) => (
        <span className="flex min-w-0 flex-col md:max-w-[320px]">
          <span className="truncate text-[13.5px] font-medium text-ink" title={checkTitle(row.original)}>
            {checkTitle(row.original)}
          </span>
          {row.original.code === "emergency_contact" && row.original.emergency_contact_name ? (
            <span className="truncate text-xs text-muted">{row.original.emergency_contact_name}</span>
          ) : (
            <span className="text-xs text-muted">{row.original.standard ? "Standard" : "Custom"}</span>
          )}
        </span>
      ),
    },
    {
      id: "due",
      header: "Due",
      cell: ({ row }) =>
        row.original.due_date ? (
          <DateText value={row.original.due_date} className={`text-[13px] ${isOverdue(row.original) ? "font-semibold text-danger" : ""}`} />
        ) : (
          <span className="text-muted">—</span>
        ),
    },
    {
      id: "owner",
      header: "Owner",
      cell: ({ row }) => <span className="text-[13px] text-body">{row.original.assignee ? (row.original.assignee.name ?? row.original.assignee.id) : "Anyone"}</span>,
    },
    {
      id: "status",
      header: "Status",
      cell: ({ row }) => (
        <span className="flex flex-col gap-1">
          <TripCheckStatusBadge status={row.original.status} overdue={isOverdue(row.original)} />
          {row.original.status === "done" && row.original.done_at && (
            <span className="text-xs text-muted">
              <DateText value={row.original.done_at} />
              {row.original.done_by?.name ? ` · ${row.original.done_by.name}` : ""}
            </span>
          )}
        </span>
      ),
    },
  ];

  return (
    <div className="flex flex-col gap-5">
      <Section
        title="Before departure"
        description="Facts the system checks for you. They turn green by themselves."
      >
        {q.isLoading ? (
          <p className="text-[13px] text-muted">Loading…</p>
        ) : (
          <ul className="grid gap-3 sm:grid-cols-2">
            {(data?.derived ?? []).map((d) => (
              <DerivedRow key={d.code} d={d} code={c.code} />
            ))}
          </ul>
        )}
      </Section>
      <Section
        title={
          <span className="flex flex-wrap items-center gap-2">
            Checks
            {data?.board && (
              <Pill tone={data.board.checks_overdue ? "danger" : data.board.checks_open ? "warning" : "success"} dot={false}>
                {data.board.checks_open ? `${data.board.checks_open} open` : "All done"}
                {data.board.checks_overdue ? ` · ${data.board.checks_overdue} overdue` : ""}
              </Pill>
            )}
          </span>
        }
        description={
          data?.trip_case.channel === "b2b"
            ? "Added when the trip was confirmed. A B2B trip has no emergency contact here: the agency keeps its travellers' details."
            : "Added when the trip was confirmed: re-check every booking, take an emergency contact, confirm travel insurance. Add your own as needed."
        }
        actions={
          canWrite && data?.writable && sold ? (
            <Button variant="primary" onClick={() => setAdding(true)}>
              <Plus size={14} /> Add check
            </Button>
          ) : null
        }
      >
        <DataTable<TripCheck>
          searchable={false}
          tableId="case-trip-checks"
          data={data?.items ?? []}
          columns={columns}
          getRowId={(r) => r.id}
          loading={q.isLoading}
          error={q.isError ? (q.error instanceof ApiError ? q.error.message : "Could not load the checks") : null}
          onRetry={() => q.refetch()}
          onRowClick={(r) => setOpenId(r.id)}
          emptyTitle={sold ? "No check on this trip" : "Checks start once the trip is confirmed"}
          emptyDescription={sold ? "The standard checks are added when a trip is confirmed." : "Approve a proposal version to confirm the trip."}
          tableMinWidth={640}
        />
      </Section>
      <CheckDrawer check={open} open={!!open} onClose={() => setOpenId(null)} writable={!!data?.writable} />
      <NewCheckDrawer caseCode={c.code} open={adding} onClose={() => setAdding(false)} />
    </div>
  );
}

function DerivedRow({ d, code }: { d: DerivedCheck; code: string }) {
  const icon = !d.available ? (
    <Clock size={18} className="text-muted2" aria-hidden="true" />
  ) : d.status === "done" ? (
    <CheckCircle2 size={18} className="text-success" aria-hidden="true" />
  ) : (
    <Circle size={18} className="text-warning" aria-hidden="true" />
  );
  let detail: React.ReactNode;
  if (d.code === "suppliers_confirmed") {
    detail =
      d.detail === "none" ? (
        "Nothing to book with suppliers."
      ) : d.status === "done" ? (
        "Every supplier booking is confirmed."
      ) : (
        <>
          Some bookings still wait for the supplier:{" "}
          <Link href={`/cases/${encodeURIComponent(code)}?tab=suppliers`} className="font-medium text-accent-deep hover:underline">
            Suppliers tab
          </Link>
          .
        </>
      );
  } else {
    detail = d.available ? (d.status === "done" ? "Payment received and verified." : "Payment not verified yet.") : "Shown once invoices and payments are recorded in Booking.";
  }
  return (
    <li className="flex items-start gap-3 rounded-md border border-line px-3 py-2.5">
      <span className="mt-0.5 shrink-0">{icon}</span>
      <span className="min-w-0">
        <span className="block text-[13px] font-medium text-ink">{d.title}</span>
        <span className="block text-xs text-muted">{detail}</span>
      </span>
    </li>
  );
}
