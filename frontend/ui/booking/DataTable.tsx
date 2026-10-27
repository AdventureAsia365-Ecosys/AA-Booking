"use client";
// ui/booking/DataTable.tsx
// AA-996 — the Booking list table: the kit DataTable (ui/DataTable.tsx, a byte-comparable CIS copy, never edited here)
// plus a compact density for zoomed laptops. Every page imports `DataTable` from "@/ui", which is this wrapper, so the
// density applies to every list without a per-page change.
//
// Compact density = a window from 768 px (below that the kit draws phone cards) to 1199 px CSS wide, e.g. a 1440 px
// laptop at 125-150 % browser zoom (Nghiệp's real setup: 150 % ≈ 960 px). At 1200 px and wider nothing changes.
//   - Smaller cell paddings (app/globals.css `.bk-dt`, pure CSS, so nothing shifts after hydration); the shell turns
//     the sidebar into its icon rail and narrows the page gutter in the same window (also CSS).
//   - Columns marked `meta: { compact: "drawer" }` (FX amounts, audit facts such as "Updated" / "Recorded by",
//     secondary counts) leave the table and show in the row's details drawer, opened by a details button in a narrow
//     column placed before the row menu / open column (or last). Every value stays one click away.
//   - The page's `tableMinWidth` (sized for 1400-1440 px) is replaced by `compactMinWidth` (default none: the table
//     takes its frame and its columns share it).
// In compact density the CSV export and the Columns menu cover the columns the table shows (the kit reads them from the
// table); at 1200 px and wider they are unchanged.

import { flexRender, getCoreRowModel, useReactTable, type ColumnDef, type RowData } from "@tanstack/react-table";
import { PanelRightOpen } from "lucide-react";
import { useMemo, useState } from "react";
import { DataTable as KitDataTable, type DataTableProps } from "../DataTable";
import { Drawer } from "../Drawer";
import { useMediaQuery } from "../useMediaQuery";

declare module "@tanstack/react-table" {
  // TanStack's documented way to type column meta (module augmentation); both type parameters are required by it.
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  interface ColumnMeta<TData extends RowData, TValue> {
    /** Compact density (768-1199 px): this column leaves the table and shows in the row's details drawer. */
    compact?: "drawer";
  }
}

/** The compact density window (keep in step with the `.bk-dt` / shell rules in app/globals.css). */
export const COMPACT_QUERY = "(min-width: 768px) and (max-width: 1199.98px)";

/** True in the compact density window (768-1199 px CSS wide). */
export function useIsCompact(): boolean {
  return useMediaQuery(COMPACT_QUERY);
}

/** Trailing columns that stay last (the row menu / open arrow): the details button goes before them. */
const TRAILING_IDS = new Set(["actions", "open", "menu"]);
const DETAILS_ID = "__details";

export type BookingDataTableProps<T> = DataTableProps<T> & {
  /** Minimum table width in compact density (default: none, the table fits its frame). */
  compactMinWidth?: number;
};

export function DataTable<T>({ columns, tableMinWidth, compactMinWidth, ...rest }: BookingDataTableProps<T>) {
  const compact = useIsCompact();
  const [details, setDetails] = useState<{ row: T } | null>(null);
  const folded = useMemo(() => columns.filter((c) => c.meta?.compact === "drawer"), [columns]);
  const shown = useMemo(() => {
    if (!compact || folded.length === 0) return columns;
    const kept = columns.filter((c) => c.meta?.compact !== "drawer");
    const button: ColumnDef<T, unknown> = {
      id: DETAILS_ID,
      header: "",
      enableSorting: false,
      enableHiding: false,
      cell: ({ row }) => <DetailsButton label={folded.map(headerText).join(", ")} onOpen={() => setDetails({ row: row.original })} />,
    };
    const last = kept[kept.length - 1];
    const trailing = last && (TRAILING_IDS.has(last.id ?? "") || last.header === "");
    return trailing ? [...kept.slice(0, -1), button, last] : [...kept, button];
  }, [columns, compact, folded]);

  return (
    <div className="bk-dt">
      <KitDataTable<T> {...rest} columns={shown} tableMinWidth={compact ? compactMinWidth : tableMinWidth} />
      {compact && details && folded.length > 0 && (
        <RowDetails row={details.row} title={columns.find((c) => c.meta?.compact !== "drawer")} folded={folded} onClose={() => setDetails(null)} />
      )}
    </div>
  );
}

function headerText<T>(c: ColumnDef<T, unknown>): string {
  return typeof c.header === "string" && c.header ? c.header : (c.id ?? "");
}

function DetailsButton({ label, onOpen }: { label: string; onOpen: () => void }) {
  return (
    <button
      type="button"
      data-testid="bk-dt-details"
      aria-label={`Show details (${label})`}
      title={`Details: ${label}`}
      onClick={(e) => {
        e.stopPropagation();
        onOpen();
      }}
      className="grid h-8 w-8 place-items-center rounded-md border border-line text-muted hover:border-accent-border hover:text-accent-deep"
    >
      <PanelRightOpen size={15} />
    </button>
  );
}

/** The row's folded columns, rendered with their own cell renderers through a one-row table (so a cell that reads
 *  `getValue()` or the row model works exactly as in the list). The first shown column heads the drawer. */
function RowDetails<T>({
  row,
  title,
  folded,
  onClose,
}: {
  row: T;
  title: ColumnDef<T, unknown> | undefined;
  folded: ColumnDef<T, unknown>[];
  onClose: () => void;
}) {
  const cols = useMemo(() => (title ? [title, ...folded] : folded), [title, folded]);
  const data = useMemo(() => [row], [row]);
  // The React Compiler skips TanStack's useReactTable (the kit table has the same rule off in eslint.config.mjs); this
  // drawer is small and re-renders only while open.
  // eslint-disable-next-line react-hooks/incompatible-library
  const table = useReactTable<T>({ data, columns: cols, getCoreRowModel: getCoreRowModel() });
  const cells = table.getRowModel().rows[0]?.getVisibleCells() ?? [];
  const head = title ? cells[0] : undefined;
  const body = title ? cells.slice(1) : cells;
  return (
    <Drawer open onClose={onClose} title="Details" width={420}>
      <div data-testid="bk-dt-details-drawer" className="flex flex-col gap-4">
        {head && <div className="text-[14px] font-semibold text-ink">{flexRender(head.column.columnDef.cell, head.getContext())}</div>}
        <dl className="flex flex-col gap-3">
          {body.map((cell) => (
            <div key={cell.id} className="flex items-baseline justify-between gap-4 border-b border-line2 pb-2">
              <dt className="shrink-0 text-[11px] font-semibold uppercase tracking-[0.06em] text-muted">{headerText(cell.column.columnDef)}</dt>
              <dd className="min-w-0 text-right text-[13px] text-body">{flexRender(cell.column.columnDef.cell, cell.getContext())}</dd>
            </div>
          ))}
        </dl>
      </div>
    </Drawer>
  );
}
