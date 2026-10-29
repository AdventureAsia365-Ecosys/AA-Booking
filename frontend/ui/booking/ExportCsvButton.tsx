"use client";
// ui/booking/ExportCsvButton.tsx
// AA-996 (audit slice 1, deviation 9) — the CSV button of a list that has no kit DataTable toolbar (the Countries card
// grid), drawn like the kit's own toolbar button so every list shows the same "CSV" tool. Put it in the FilterBar
// `tools` slot. The file is built with the kit helpers (ui/csv.ts `toCsv` + `downloadCsv`, UTF-8 BOM for Excel), every
// value through the spreadsheet formula guard (csvCell.ts).

import { Download } from "lucide-react";
import { downloadCsv, toCsv } from "../core/csv";
import { safeCsvColumns } from "./csvCell";
import { K, RADIUS, sans } from "../core/tokens";

export function ExportCsvButton<T>({
  rows,
  columns,
  filename,
  disabled,
}: {
  rows: T[];
  columns: { header: string; value: (row: T) => unknown }[];
  filename: string;
  disabled?: boolean;
}) {
  const off = disabled || rows.length === 0;
  return (
    <button
      type="button"
      data-testid="bk-export-csv"
      disabled={off}
      onClick={() => downloadCsv(filename, toCsv(rows, safeCsvColumns(columns)))}
      style={{
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
        cursor: off ? "default" : "pointer",
        opacity: off ? 0.5 : 1,
        fontFamily: sans,
        whiteSpace: "nowrap",
      }}
    >
      <Download size={13} /> CSV
    </button>
  );
}
