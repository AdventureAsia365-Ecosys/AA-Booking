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
//   - The CSV export does not depend on the density (review fix): when columns are folded, the kit's own CSV button is
//     replaced by the wrapper's, which exports every column of the page (the kit's header / accessor rule) over the
//     rows the page passes, so a file exported at 960 px equals the one exported at 1440 px. The Columns menu lists the
//     columns the table shows (the folded ones are in the drawer).

import { flexRender, getCoreRowModel, useReactTable, type ColumnDef, type RowData } from "@tanstack/react-table";
import { Download, PanelRightOpen } from "lucide-react";
import { useMemo, useState } from "react";
import { downloadCsv, toCsv } from "../csv";
import { DataTable as KitDataTable, type DataTableProps } from "../DataTable";
import { Drawer } from "../Drawer";
import { K, RADIUS, sans } from "../tokens";
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
  const { data, getRowId, enableCsv = false, csvFilename = "export", toolbarExtra } = rest;
  // The open drawer's row, by id: resolved from the current `data` at render, so a refetch or a mutation shows fresh
  // values, and a row that left the data (page, filter, delete) closes the drawer.
  const [detailsId, setDetailsId] = useState<string | null>(null);
  // Leaving the compact window closes the drawer for good (otherwise it would reopen unasked when the window narrows
  // or the zoom comes back). Render-time state adjustment, React's documented alternative to an effect.
  if (!compact && detailsId !== null) setDetailsId(null);
  const folded = useMemo(() => columns.filter((c) => c.meta?.compact === "drawer"), [columns]);
  const folding = compact && folded.length > 0;
  const shown = useMemo(() => {
    if (!folding) return columns;
    const kept = columns.filter((c) => c.meta?.compact !== "drawer");
    const button: ColumnDef<T, unknown> = {
      id: DETAILS_ID,
      header: "",
      enableSorting: false,
      enableHiding: false,
      cell: ({ row }) => <DetailsButton label={folded.map(headerText).join(", ")} onOpen={() => setDetailsId(getRowId(row.original))} />,
    };
    const last = kept[kept.length - 1];
    const trailing = last && (TRAILING_IDS.has(last.id ?? "") || last.header === "");
    return trailing ? [...kept.slice(0, -1), button, last] : [...kept, button];
  }, [columns, folding, folded, getRowId]);
  const detailsRow = folding && detailsId !== null ? data.find((r) => getRowId(r) === detailsId) : undefined;
  // While columns are folded the kit's CSV would export only the shown columns: the wrapper's button exports them all.
  const ownCsv = folding && enableCsv;

  return (
    <div className="bk-dt">
      <KitDataTable<T>
        {...rest}
        columns={shown}
        tableMinWidth={compact ? compactMinWidth : tableMinWidth}
        enableCsv={ownCsv ? false : enableCsv}
        toolbarExtra={
          ownCsv ? (
            <>
              {toolbarExtra}
              <CsvButton onClick={() => downloadCsv(csvFilename, toCsv(data, csvColumns(columns)))} />
            </>
          ) : (
            toolbarExtra
          )
        }
      />
      {detailsRow !== undefined && (
        <RowDetails row={detailsRow} title={columns.find((c) => c.meta?.compact !== "drawer")} folded={folded} onClose={() => setDetailsId(null)} />
      )}
    </div>
  );
}

/** The kit's CSV column rule (ui/DataTable.tsx `exportCsv`: header text or column id; accessorFn, else accessorKey,
 *  else empty), applied to every column of the page, so the file is the one the kit exports at 1200 px and wider. */
export function csvColumns<T>(columns: ColumnDef<T, unknown>[]): { header: string; value: (row: T) => unknown }[] {
  return columns
    .filter((c) => c.id !== "__select")
    .map((c) => {
      const def = c as { accessorFn?: (r: T, i: number) => unknown; accessorKey?: string };
      return {
        header: typeof c.header === "string" ? c.header : (c.id ?? def.accessorKey ?? ""),
        value: (row: T) => {
          if (def.accessorFn) return def.accessorFn(row, 0);
          if (def.accessorKey) return (row as Record<string, unknown>)[def.accessorKey];
          return "";
        },
      };
    });
}

/** Same look as the kit's CSV button; `order: 1` puts it last in the toolbar, where the kit draws its own. */
function CsvButton({ onClick }: { onClick: () => void }) {
  return (
    <button
      type="button"
      data-testid="bk-dt-csv"
      onClick={onClick}
      style={{
        order: 1,
        display: "inline-flex",
        alignItems: "center",
        gap: 6,
        padding: "7px 12px",
        borderRadius: RADIUS.pill,
        border: `1px solid ${K.line}`,
        background: K.card,
        color: K.ink3,
        fontSize: 12,
        fontWeight: 600,
        cursor: "pointer",
        fontFamily: sans,
      }}
    >
      <Download size={13} /> CSV
    </button>
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
