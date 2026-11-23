"use client";
// app/(staff)/_parts/MyWorkBand.tsx
// AA-1159 — the My work band of the staff dashboard (the Booking half of My work; the CIS half is /admin/my-work).
// GET /booking/my-work/summary answers one tile per work queue the signed-in role's capabilities allow (by capability,
// never by person): the count, the oldest item or the earliest due date, and the filter of the list it was counted
// from. Each tile opens that list with that filter (lib/myWork.ts tileHref), so the tile and the list show the same
// total. Two groups: "To do" (work queues) and "To approve or pay" (approvals). A role with none of the capabilities
// sees no band. Loaded on open and on Refresh, never polled.

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import {
  AlarmClock,
  Banknote,
  Briefcase,
  CalendarClock,
  FileWarning,
  Handshake,
  Landmark,
  OctagonX,
  Power,
  ReceiptText,
  RefreshCw,
  SquareCheck,
  Stamp,
  UserCheck,
} from "lucide-react";
import { CIS_MY_WORK_PATH } from "@/lib/config";
import { myWorkApi, type MyWorkTile } from "@/lib/api/myWork";
import { MY_WORK_TILES, hasMyWork, tileHref, tileSpec, type MyWorkGroup, type MyWorkTileSpec } from "@/lib/myWork";
import { Button, DateText, ErrorState, KpiTile, ManagedInCisLink, Section, useIsPhone, useStaff } from "@/ui";
import type { Tone } from "@/ui/core/primitives";
import { retryUnlessClientError } from "@/ui/booking/QueryProvider";
import { useT } from "@/ui/core/useI18n";
import { useTx } from "@/ui/local/useTx";
import { MSG } from "./MyWorkBand.messages";

const ICON = 16;
const ICONS: Record<string, React.ReactNode> = {
  my_open_cases: <Briefcase size={ICON} />,
  trip_checks_overdue: <SquareCheck size={ICON} />,
  b2b_on_request: <Handshake size={ICON} />,
  supplier_bills_overdue: <FileWarning size={ICON} />,
  supplier_bills_due_soon: <CalendarClock size={ICON} />,
  agency_invoices_overdue: <AlarmClock size={ICON} />,
  supplier_bills_to_approve: <Stamp size={ICON} />,
  supplier_bills_to_pay: <Banknote size={ICON} />,
  agency_payments_to_approve: <ReceiptText size={ICON} />,
  stop_sales_to_review: <OctagonX size={ICON} />,
  suppliers_to_activate: <Power size={ICON} />, // AA-1158
  billing_changes_to_approve: <Landmark size={ICON} />,
  owner_self_approvals: <UserCheck size={ICON} />,
};
/** A count above zero on these tiles is late work (red hint); on the approval tiles it waits for the viewer (amber). */
const LATE = new Set(["trip_checks_overdue", "supplier_bills_overdue", "agency_invoices_overdue"]);
const GROUPS: readonly MyWorkGroup[] = ["work", "approve"];

export function MyWorkBand() {
  const tr = useT(MSG);
  const staff = useStaff();
  const show = hasMyWork(staff.permissions);
  const q = useQuery({
    queryKey: ["my-work", "summary"],
    queryFn: myWorkApi.summary,
    enabled: show,
    retry: retryUnlessClientError,
  });
  if (!show) return null;

  // While loading, the tiles the role may see (skeletons); then the API's tiles in its order.
  const rows: { spec: MyWorkTileSpec; tile: MyWorkTile | undefined }[] = q.data
    ? q.data.tiles.flatMap((tile) => {
        const spec = tileSpec(tile.key);
        return spec ? [{ spec, tile }] : [];
      })
    : MY_WORK_TILES.filter((s) => staff.permissions.includes(s.capability)).map((spec) => ({ spec, tile: undefined }));

  return (
    <Section
      testId="my-work-band"
      title={tr("My work")}
      description={tr("The queues your permissions let you work. Each tile opens the list it counts.")}
      actions={
        <span className="flex flex-wrap items-center gap-3">
          {staff.cis_role ? <ManagedInCisLink path={CIS_MY_WORK_PATH} label="CIS: My work" /> : null}
          <Button size="sm" variant="ghost" onClick={() => q.refetch()} disabled={q.isFetching} testId="my-work-refresh">
            <span className="inline-flex items-center gap-1.5">
              <RefreshCw size={13} /> {tr("Refresh")}
            </span>
          </Button>
        </span>
      }
    >
      {q.isError ? (
        <ErrorState message={tr("My work could not load. {0}", (q.error as Error).message)} onRetry={() => q.refetch()} />
      ) : (
        <div className="flex flex-col gap-4">
          {GROUPS.map((key) => {
            const inGroup = rows.filter((r) => r.spec.group === key);
            if (!inGroup.length) return null;
            return (
              <div key={key} className="flex flex-col gap-2" data-testid={`my-work-group-${key}`}>
                <h3 className="text-xs font-semibold uppercase tracking-[0.06em] text-muted">{key === "work" ? tr("To do") : tr("To approve or pay")}</h3>
                {/* One column on a phone: the tile labels are long, two columns of 390 px would cut them (so not the
                    shell's 2-up aa-kpi-grid). */}
                <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 sm:gap-4 lg:grid-cols-4">
                  {inGroup.map(({ spec, tile }) => (
                    <MyWorkTileLink key={spec.key} spec={spec} tile={tile} />
                  ))}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </Section>
  );
}

/** undefined: loading (skeleton); null: not available ("—"); else the count. */
function tileValue(tile: MyWorkTile | undefined): number | null | undefined {
  if (!tile) return undefined;
  return tile.available ? tile.count : null;
}

/** Late work in red, approvals waiting for the viewer in amber, the rest (and nothing waiting) neutral. */
function tileTone(spec: MyWorkTileSpec, count: number | null | undefined): Tone {
  if (typeof count !== "number" || count <= 0) return "neutral";
  if (LATE.has(spec.key)) return "danger";
  return spec.group === "approve" ? "warning" : "neutral";
}

function MyWorkTileLink({ spec, tile }: { spec: MyWorkTileSpec; tile: MyWorkTile | undefined }) {
  const tx = useTx();
  const isPhone = useIsPhone();
  const href = tileHref(spec.path, tile?.filter ?? {});
  const count = tileValue(tile);
  // S286 F5: the kit tile cuts its label to one line with an ellipsis; in the 4-column grid at 1440 px that cut
  // "Agency payments to approve". The band lets the label wrap instead (an override from outside the kit, which
  // stays untouched): the label row is the first child of the tile root, its text the span carrying the title.
  return (
    <Link
      href={href}
      aria-label={tx(spec.aria)}
      data-testid={`my-work-tile-${spec.key}`}
      className="block min-w-0 rounded-lg outline-none [&>div]:h-full [&>div>div:first-child_span[title]]:whitespace-normal! [&>div>div:first-child_span[title]]:overflow-visible! transition hover:-translate-y-px hover:shadow-kit-md focus-visible:ring-2 focus-visible:ring-accent-border"
    >
      <KpiTile
        size={isPhone ? "sm" : "md"}
        label={tx(spec.label)}
        icon={ICONS[spec.key]}
        value={count}
        hint={tile ? <TileHint spec={spec} tile={tile} /> : undefined}
        hintTone={tileTone(spec, count)}
      />
    </Link>
  );
}

function TileHint({ spec, tile }: { spec: MyWorkTileSpec; tile: MyWorkTile }) {
  const tr = useT(MSG);
  const tx = useTx();
  if (!tile.available) return <>{tr("Not available yet")}</>;
  if (!tile.count) return <>{tx(spec.empty)}</>;
  if (tile.earliest_due) {
    return (
      <>
        {tile.days != null ? tr("Within {0} days, first due", tile.days) : tr("Earliest due")} <DateText value={tile.earliest_due} />
      </>
    );
  }
  if (tile.oldest_at) {
    return (
      <>
        {tr("Oldest")} <DateText value={tile.oldest_at} relative />
      </>
    );
  }
  return null;
}
