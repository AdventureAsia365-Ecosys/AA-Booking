"use client";
// ui/booking/LightTable.tsx
// AA-996 (Figma View Details: the invoice's one-line items table) — a light table for the short, fixed lists of one
// invoice (lines, payment schedule, payments): no Columns menu, no pager, no search, padded inside its card, numbers
// right-aligned, and a `footer` slot under the rows (the totals). Built as an ARIA table on a CSS grid (like the audit
// log's before / after grid), not a raw <table>: the page rule keeps <table> for the kit DataTable, which stays the
// component for real lists. Under 640 px each row becomes a stacked card with the column names as labels, so a phone
// never scrolls sideways.

import type { ReactNode } from "react";

export interface LightColumn<T> {
  id: string;
  header: string;
  cell: (row: T) => ReactNode;
  /** Right-align the column (money, quantities). */
  numeric?: boolean;
  /** Grid track of the column (default `minmax(0,1fr)`), e.g. `minmax(0,2.4fr)` for a description. */
  width?: string;
  /** Hide the label on the phone card (e.g. the main description, or an actions cell). */
  phoneLabel?: false;
}

export function LightTable<T>({
  rows,
  columns,
  rowKey,
  empty,
  footer,
  label,
}: {
  rows: T[];
  columns: LightColumn<T>[];
  rowKey: (row: T) => string;
  /** Shown instead of the rows when there is none. */
  empty?: ReactNode;
  /** Rendered under the rows (inside the card padding). */
  footer?: ReactNode;
  /** Accessible name of the table. */
  label: string;
}) {
  const tracks = columns.map((c) => c.width ?? "minmax(0,1fr)").join(" ");
  const align = (c: LightColumn<T>) => (c.numeric ? "justify-self-end text-right" : "text-left");
  return (
    <div className="px-5 py-4" data-testid="light-table">
      {rows.length === 0 ? (
        <div className="py-3 text-[13px] text-muted">{empty ?? "Nothing yet."}</div>
      ) : (
        <>
          <div role="table" aria-label={label} className="hidden text-[13px] sm:block">
            <div role="rowgroup">
              <div role="row" className="grid gap-x-4 border-b border-line pb-2.5" style={{ gridTemplateColumns: tracks }}>
                {columns.map((c) => (
                  <span
                    key={c.id}
                    role="columnheader"
                    className={`min-w-0 text-[11px] font-semibold uppercase tracking-[0.06em] text-muted ${align(c)}`}
                  >
                    {c.header}
                  </span>
                ))}
              </div>
            </div>
            <div role="rowgroup">
              {rows.map((r) => (
                <div
                  key={rowKey(r)}
                  role="row"
                  className="grid items-start gap-x-4 border-b border-line2 py-3 last:border-b-0"
                  style={{ gridTemplateColumns: tracks }}
                >
                  {columns.map((c) => (
                    <span key={c.id} role="cell" className={`min-w-0 ${align(c)}`}>
                      {c.cell(r)}
                    </span>
                  ))}
                </div>
              ))}
            </div>
          </div>
          <ul aria-label={label} className="flex flex-col gap-3 sm:hidden">
            {rows.map((r) => (
              <li key={rowKey(r)} className="flex flex-col gap-1.5 rounded-md border border-line2 bg-panel px-3 py-2.5 text-[13px]">
                {columns.map((c) =>
                  c.phoneLabel === false ? (
                    <div key={c.id} className="min-w-0">
                      {c.cell(r)}
                    </div>
                  ) : (
                    <div key={c.id} className="flex min-w-0 items-baseline justify-between gap-3">
                      <span className="shrink-0 text-[11px] font-semibold uppercase tracking-[0.06em] text-muted">{c.header}</span>
                      <span className="min-w-0 text-right">{c.cell(r)}</span>
                    </div>
                  ),
                )}
              </li>
            ))}
          </ul>
        </>
      )}
      {footer}
    </div>
  );
}
