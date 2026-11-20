"use client";
// app/(staff)/stop-sales/_parts/StopSalesView.tsx — Supply → Stop-sales (AA-1159; stop-sales AA-1145, bk-060, T13).
// The stop-sales on supplier contracts that wait for a second person, across every supplier, the longest waiting
// first: a pending one (approve / reject with a reason) or an approved one whose end was asked (approve lifting it).
// GET /booking/supply/restrictions?kind=stop_sale&review=waiting|me (supply.read). `?review=me` (the My work tile's
// view) keeps only what the viewer may decide: never a stop-sale they entered or asked to end (maker-checker, the API
// refuses those with 403 maker_checker). Deciding needs stop_sale.approve (and stop_sale.write for approve / reject).

import { useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { ApiError } from "@/lib/api/client";
import { componentLabel, requester, stopSalesApi, waitsFor, type StopSale, type StopSaleReview } from "@/lib/api/stopSales";
import {
  Button,
  ConfirmDialog,
  DataTable,
  DateText,
  EmptyState,
  Field,
  PageFrame,
  Pill,
  Section,
  Select,
  useCan,
  useStaff,
  useToast,
  useUrlFilters,
} from "@/ui";
import { useT } from "@/ui/core/useI18n";
import { MSG } from "./StopSalesView.messages";

function errorText(e: unknown): string {
  return e instanceof ApiError || e instanceof Error ? e.message : "Something went wrong";
}

export function StopSalesView() {
  const t = useT(MSG);
  const canRead = useCan("supply.read");
  return (
    <PageFrame
      title={t("Stop-sales")}
      description={t(
        "Stop-sales on supplier contracts that wait for a second person. A stop-sale closes the dates the moment it is entered; another person approves or rejects it, and lifting one also takes a second person.",
      )}
    >
      {canRead ? (
        <StopSalesList />
      ) : (
        <EmptyState title={t("No access")} description={t("Stop-sales need the supply reader permission.")} />
      )}
    </PageFrame>
  );
}

function StopSalesList() {
  const t = useT(MSG);
  const { get, set } = useUrlFilters();
  const review: StopSaleReview = get("review") === "me" ? "me" : "waiting";
  const me = useStaff();
  const canApprove = useCan("stop_sale.approve");
  const canWrite = useCan("stop_sale.write");
  const canDecide = canApprove && canWrite;
  const toast = useToast();
  const qc = useQueryClient();
  const [rejecting, setRejecting] = useState<StopSale | null>(null);
  const list = useQuery({ queryKey: ["stop-sales", review], queryFn: () => stopSalesApi.list(review) });
  const done = (message: string) => {
    toast.success(message);
    setRejecting(null);
    qc.invalidateQueries({ queryKey: ["stop-sales"] });
    qc.invalidateQueries({ queryKey: ["my-work"] });
  };
  const approve = useMutation({
    mutationFn: (s: StopSale) => (waitsFor(s) === "review" ? stopSalesApi.approve(s.id) : stopSalesApi.approveEnd(s.id)),
    onSuccess: (_, s) => done(waitsFor(s) === "review" ? t("Stop-sale approved") : t("Stop-sale lifted")),
    onError: (e) => toast.error(errorText(e)),
  });
  const reject = useMutation({
    mutationFn: ({ s, reason }: { s: StopSale; reason: string }) => stopSalesApi.reject(s.id, reason),
    onSuccess: () => done(t("Stop-sale rejected")),
    onError: (e) => toast.error(errorText(e)),
  });

  const columns: ColumnDef<StopSale, unknown>[] = [
    {
      id: "supplier",
      header: t("Supplier"),
      cell: ({ row }) => (
        <span className="flex min-w-0 flex-col">
          <Link href={`/suppliers/${row.original.supplier_code}`} className="truncate font-semibold" title={row.original.supplier_name}>
            {row.original.supplier_name}
          </Link>
          <span className="block max-w-[200px] truncate text-xs text-muted" title={row.original.contract_title}>
            {row.original.contract_title}
          </span>
        </span>
      ),
    },
    {
      id: "closes",
      header: t("Closes"),
      cell: ({ row }) => (
        <span className="block max-w-[220px] truncate text-[13px] text-body" title={componentLabel(row.original)}>
          {componentLabel(row.original)}
        </span>
      ),
    },
    {
      id: "dates",
      header: t("Dates"),
      cell: ({ row }) => (
        <span className="whitespace-nowrap text-[13px]">
          <DateText value={row.original.date_from} /> – <DateText value={row.original.date_to} />
        </span>
      ),
    },
    {
      id: "why",
      header: t("Why"),
      cell: ({ row }) => {
        const why = waitsFor(row.original) === "review" ? row.original.note : row.original.end_reason;
        return (
          <span className="block max-w-[240px] truncate text-[13px] text-body" title={why ?? ""}>
            {why ?? "—"}
          </span>
        );
      },
    },
    {
      id: "waits",
      header: t("Waits for"),
      cell: ({ row }) =>
        waitsFor(row.original) === "review" ? <Pill tone="warning">{t("Review")}</Pill> : <Pill tone="info">{t("Lifting it")}</Pill>,
    },
    { id: "by", header: t("Asked by"), cell: ({ row }) => requester(row.original).name },
    {
      id: "since",
      header: t("Waiting since"),
      cell: ({ row }) => (
        <DateText value={waitsFor(row.original) === "review" ? row.original.created_at : (row.original.end_requested_at ?? row.original.created_at)} />
      ),
    },
    {
      id: "actions",
      header: "",
      cell: ({ row }) => {
        const s = row.original;
        if (!canApprove) return null;
        if (requester(s).id === me.cis_user_id) return <span className="text-[12px] text-muted">{t("Another person decides")}</span>;
        if (waitsFor(s) === "end") {
          return (
            <Button size="sm" variant="primary" disabled={approve.isPending} onClick={() => approve.mutate(s)}>
              {t("Approve lifting")}
            </Button>
          );
        }
        if (!canDecide) return null;
        return (
          <span className="flex gap-2">
            <Button size="sm" variant="primary" disabled={approve.isPending} onClick={() => approve.mutate(s)}>
              {t("Approve")}
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setRejecting(s)}>
              {t("Reject")}
            </Button>
          </span>
        );
      },
    },
  ];

  return (
    <Section title={t("Waiting for review")} description={list.data ? t("{0} waiting", list.data.total) : undefined}>
      <div className="mb-3 flex gap-3">
        <Field label={t("Show")}>
          <Select
            value={review}
            onChange={(e) => set("review", e.target.value === "me" ? "me" : null)}
            options={[
              { value: "waiting", label: t("Everything waiting") },
              { value: "me", label: t("Only what I can decide") },
            ]}
          />
        </Field>
      </div>
      <DataTable<StopSale>
        searchable={false}
        tableId="stop-sales"
        data={list.data?.items ?? []}
        columns={columns}
        getRowId={(r) => r.id}
        loading={list.isLoading}
        error={list.isError ? errorText(list.error) : null}
        onRetry={() => list.refetch()}
        emptyTitle={t("Nothing waits for review")}
        emptyDescription={t("A stop-sale is entered on a published supplier contract; it shows here until a second person decides it.")}
        tableMinWidth={1100}
      />
      <ConfirmDialog
        open={rejecting !== null}
        onClose={() => setRejecting(null)}
        onConfirm={(reason) => rejecting && reject.mutate({ s: rejecting, reason })}
        title={t("Reject stop-sale")}
        message={t("The dates open for sale again. Say why (for example: the supplier did not close them).")}
        confirmLabel={t("Reject")}
        destructive
        reason={{ label: t("Reason"), required: true, maxLength: 300 }}
        busy={reject.isPending}
      />
    </Section>
  );
}
