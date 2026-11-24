"use client";
// app/(staff)/add-ons/_parts/AddonsView.tsx — Supply → Add-ons (AA-1151, bk-068; PR-29 B1 / B3 / T7).
// Optional activities AA sells per place and day: one Booking service of an AA supplier with its places and sale
// rules. Content staff and contracting write drafts (or import a file of activities); a second person publishes a draft
// (never one of its writers: the API answers 403 maker_checker). The table flags what still stops an add-on from
// selling (no place, no price in force, no published customer text in CIS). Above it, the supplier contracts that end
// within the warning window of Pricing > Policy (contract_expiry_alert_days), so nobody misses a renewal (the shared
// panel of the Suppliers pages, AA-1177).

import { useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { FileSpreadsheet } from "lucide-react";
import { ApiError } from "@/lib/api/client";
import { addonsApi, weekdaysLabel, wroteIt, type AddonOffer, type AddonStatus } from "@/lib/api/addons";
import {
  Button,
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
import { ExpiringContracts } from "@/app/(staff)/suppliers/_parts/ExpiringContracts";
import { ActivityImportDrawer } from "./ActivityImportDrawer";
import { MSG } from "./AddonsView.messages";

function errorText(e: unknown): string {
  return e instanceof ApiError || e instanceof Error ? e.message : "Something went wrong";
}

const STATUS_TONE: Record<AddonStatus, "warning" | "success" | "neutral"> = {
  draft: "warning",
  published: "success",
  retired: "neutral",
};

export function AddonsView() {
  const t = useT(MSG);
  const canRead = useCan("addon.read");
  const canSupplyWrite = useCan("supply.write");
  const canAddonWrite = useCan("addon.write");
  const canImport = canSupplyWrite && canAddonWrite;
  const [importing, setImporting] = useState(false);
  return (
    <PageFrame
      title={t("Add-ons")}
      description={t(
        "Optional activities AA sells per place and day, always with an AA supplier. Content staff and contracting prepare a draft; a second person publishes it. A day without a price in force is not offered.",
      )}
      actions={
        canImport ? (
          <Button variant="secondary" onClick={() => setImporting(true)}>
            <FileSpreadsheet size={14} /> {t("Import activities")}
          </Button>
        ) : undefined
      }
    >
      {canRead ? (
        <>
          <ExpiringContracts />
          <AddonsList />
        </>
      ) : (
        <EmptyState title={t("No access")} description={t("Add-ons need the add-on reader permission.")} />
      )}
      {importing && <ActivityImportDrawer onClose={() => setImporting(false)} />}
    </PageFrame>
  );
}

function AddonsList() {
  const t = useT(MSG);
  const { get, set } = useUrlFilters();
  const status = (["draft", "published", "retired"] as const).find((s) => s === get("status"));
  const approvable = get("approvable") === "me" ? "me" : undefined;
  const me = useStaff();
  const canWrite = useCan("addon.write");
  const canApprove = useCan("addon.approve");
  const toast = useToast();
  const qc = useQueryClient();
  const list = useQuery({
    queryKey: ["add-ons", status ?? "all", approvable ?? ""],
    queryFn: () => addonsApi.list({ status, approvable, limit: 200 }),
  });
  const act = useMutation({
    mutationFn: ({ offer, action }: { offer: AddonOffer; action: "publish" | "reopen" | "retire" }) =>
      addonsApi[action](offer.id),
    onSuccess: (_, { action }) => {
      toast.success(
        action === "publish" ? t("Add-on published") : action === "reopen" ? t("Add-on back to draft") : t("Add-on retired"),
      );
      qc.invalidateQueries({ queryKey: ["add-ons"] });
      qc.invalidateQueries({ queryKey: ["my-work"] });
    },
    onError: (e) => toast.error(errorText(e)),
  });

  const statusLabel: Record<AddonStatus, string> = { draft: t("draft"), published: t("published"), retired: t("retired") };
  const weekday: Record<string, string> = {
    Daily: t("Daily"), Mon: t("Mon"), Tue: t("Tue"), Wed: t("Wed"), Thu: t("Thu"), Fri: t("Fri"), Sat: t("Sat"), Sun: t("Sun"),
  };
  const missingLabel: Record<string, string> = {
    no_place: t("No place"),
    no_price: t("No price in force"),
    service_inactive: t("Service inactive"),
    supplier_inactive: t("Supplier inactive"),
    no_content: t("No customer text"),
  };

  const columns: ColumnDef<AddonOffer, unknown>[] = [
    {
      id: "activity",
      header: t("Activity"),
      cell: ({ row }) => (
        <span className="flex min-w-0 flex-col">
          <Link href={`/services/${row.original.service_code}`} className="truncate font-semibold" title={row.original.service_title}>
            {row.original.service_title}
          </Link>
          <span className="block max-w-[220px] truncate text-xs text-muted" title={row.original.supplier_name}>
            {row.original.service_code} · {row.original.supplier_name}
          </span>
        </span>
      ),
    },
    {
      id: "places",
      header: t("Places"),
      cell: ({ row }) => {
        const names = row.original.places.map((p) => p.name ?? p.destination_id).join(", ");
        return (
          <span className="block max-w-[180px] truncate text-[13px] text-body" title={names}>
            {names || "—"}
          </span>
        );
      },
    },
    {
      id: "rules",
      header: t("Sale rules"),
      cell: ({ row }) => {
        const o = row.original;
        const times = o.start_times.length ? o.start_times.join(", ") : t("any time");
        return (
          <span className="flex min-w-0 flex-col text-[13px] text-body">
            <span>{t("{0} · {1}", weekdaysLabel(o.operating_weekdays, (w) => weekday[w] ?? w), times)}</span>
            <span className="text-xs text-muted">{t("min {0} pax · book {1} h ahead", o.min_pax, o.cutoff_hours)}</span>
          </span>
        );
      },
    },
    {
      id: "status",
      header: t("Status"),
      cell: ({ row }) => (
        <span className="flex flex-wrap gap-1">
          <Pill tone={STATUS_TONE[row.original.status]}>{statusLabel[row.original.status]}</Pill>
          {row.original.missing.map((m) => (
            <Pill key={m} tone="danger">
              {missingLabel[m] ?? m}
            </Pill>
          ))}
        </span>
      ),
    },
    {
      id: "updated",
      header: t("Changed"),
      cell: ({ row }) => <DateText className="whitespace-nowrap text-[13px]" value={row.original.updated_at} />,
    },
    {
      id: "actions",
      header: "",
      cell: ({ row }) => {
        const o = row.original;
        const busy = act.isPending;
        if (o.status === "draft") {
          return (
            <span className="flex gap-2">
              {canApprove && !wroteIt(o, me.cis_user_id) && (
                <Button size="sm" variant="primary" disabled={busy || o.missing.some((m) => m !== "no_content")}
                  onClick={() => act.mutate({ offer: o, action: "publish" })}>
                  {t("Publish")}
                </Button>
              )}
              {canApprove && wroteIt(o, me.cis_user_id) && (
                <span className="text-[12px] text-muted">{t("Another person publishes")}</span>
              )}
              {canWrite && (
                <Button size="sm" variant="ghost" disabled={busy} onClick={() => act.mutate({ offer: o, action: "retire" })}>
                  {t("Retire")}
                </Button>
              )}
            </span>
          );
        }
        if (!canWrite) return null;
        return (
          <span className="flex gap-2">
            <Button size="sm" variant="ghost" disabled={busy} onClick={() => act.mutate({ offer: o, action: "reopen" })}>
              {t("Back to draft")}
            </Button>
            {o.status === "published" && (
              <Button size="sm" variant="ghost" disabled={busy} onClick={() => act.mutate({ offer: o, action: "retire" })}>
                {t("Retire")}
              </Button>
            )}
          </span>
        );
      },
    },
  ];

  return (
    <Section title={t("Add-ons")} description={list.data ? t("{0} add-ons", list.data.total) : undefined}>
      <div className="mb-3 flex flex-wrap gap-3">
        <Field label={t("Status")}>
          <Select
            value={status ?? ""}
            onChange={(e) => set("status", e.target.value || null)}
            options={[
              { value: "", label: t("All") },
              { value: "draft", label: t("draft") },
              { value: "published", label: t("published") },
              { value: "retired", label: t("retired") },
            ]}
          />
        </Field>
        <Field label={t("Show")}>
          <Select
            value={approvable ?? ""}
            onChange={(e) => set("approvable", e.target.value === "me" ? "me" : null)}
            options={[
              { value: "", label: t("Every add-on") },
              { value: "me", label: t("Only drafts I can publish") },
            ]}
          />
        </Field>
      </div>
      <DataTable<AddonOffer>
        searchable={false}
        tableId="add-ons"
        data={list.data?.items ?? []}
        columns={columns}
        getRowId={(r) => r.id}
        loading={list.isLoading}
        error={list.isError ? errorText(list.error) : null}
        onRetry={() => list.refetch()}
        emptyTitle={t("No add-on yet")}
        emptyDescription={t("Import a file of activities, or ask contracting to add the supplier's activities and their prices.")}
        tableMinWidth={1000}
      />
    </Section>
  );
}
