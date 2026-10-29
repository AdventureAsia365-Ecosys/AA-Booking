// ui/index.ts
// AA-683 — barrel for the Booking UI. Pages import from "@/ui", never from the folders below.
//
// AA-994 S4b (root ADR 0016): three layers.
//   ui/core/     the UI kit core, a byte copy of AA-CIS-App frontend/app/_kit/core written by the root
//                tools/kit/kit-sync.sh (pinned in frontend/kit.lock.json; Frontend Build checks integrity and
//                provenance). Never edited here: a kit change is made in CIS and synced.
//   ui/local/    the Booking staff shell (sidebar, topbar, bottom nav, command palette, staff context, nav, theme
//                store bound to `bk_theme`): app-specific, inline styles on the kit tokens.
//   ui/booking/  Booking components (Tailwind utilities on the same --aa-* tokens) and pure helpers.

// ── kit core (portable) ────────────────────────────────────────────────────────────────────────────────────────
export * from "./core/tokens";
export * from "./core/brand";
export * from "./core/primitives";
export * from "./core/Modal";
export * from "./core/Drawer";
export * from "./core/Toast";
export * from "./core/csv";
export * from "./core/savedViews";
export { Providers } from "./core/Providers";
export { useSidebarCollapsed } from "./core/useSidebar";
export { useMediaQuery, useIsPhone, PHONE_QUERY } from "./core/useMediaQuery";
// AA-994 S4a parts, upstreamed from the former ui/booking copies (same props, so pages did not change).
export { Section } from "./core/Section";
export { KpiTile } from "./core/KpiTile";
export {
  FilterBar, FilterField, useUrlFilters, filterControlStyle, FILTER_CONTROL_CLASS, type FilterSelect,
} from "./core/FilterBar";
export { RowMenu, type RowMenuItem } from "./core/RowMenu";
export { DateField } from "./core/DateField";
export { Money } from "./core/Money";
export { formatMoney, minorUnits } from "./core/format";
export { DateText, relativeTime } from "./core/DateText";
// UI spec §5 name of the kit confirm dialog (ConfirmModal absorbed the Booking ConfirmDialog in S4a).
export { ConfirmModal as ConfirmDialog } from "./core/Modal";

// AA-996: `DataTable` is the Booking wrapper (compact density for zoomed laptops, CSV through the formula guard,
// `filters` in the toolbar) around the kit table.
export type { DataTableProps, ColumnDef } from "./core/DataTable";
export { createColumnHelper } from "./core/DataTable";
export { DataTable, COMPACT_QUERY, useIsCompact, csvMoney, type BookingDataTableProps, type CsvColumn } from "./booking/DataTable";

// ── Booking staff shell ────────────────────────────────────────────────────────────────────────────────────────
export { AdminShell } from "./local/AdminShell";
export { Sidebar } from "./local/Sidebar";
export { Topbar } from "./local/Topbar";
export { BottomNav } from "./local/BottomNav";
export { CommandPalette } from "./local/CommandPalette";
export * from "./local/nav";
export * from "./local/StaffContext";
export { signOut } from "./local/signOut";
export { themeStore, THEME_KEY, type ThemeChoice, type ResolvedTheme } from "./local/theme";
export { useThemeChoice, useResolvedTheme } from "./local/useTheme";

// ── Booking components (UI spec §5) ────────────────────────────────────────────────────────────────────────────
// PageFrame: the kit frame with the Booking defaults (wide column, Booking gutter); see ui/booking/PageFrame.tsx.
export { PageFrame } from "./booking/PageFrame";
export { TimeField } from "./booking/TimeField";
export * from "./booking/dates";
export * from "./booking/StatusBadge";
export * from "./booking/Stepper";
export * from "./booking/FormDrawer";
export * from "./booking/ExportCsvButton";
export * from "./booking/WaveFrame";
export * from "./booking/FromCis";
export * from "./booking/CisPhoto";
export * from "./booking/PersonCell";
export * from "./booking/Code";
export * from "./booking/NotificationPopover";
export * from "./booking/notifications";
export * from "./booking/NotificationItem";
export * from "./booking/audit";
export * from "./booking/LightTable";

// Kit loading placeholders under the spec names (UI spec §5 "Skeleton").
export { SkeletonBar as Skeleton } from "./core/primitives";

// Name clash: the kit StatusBadge maps job-runner states (CIS). In Booking, `StatusBadge` is the
// per-entity map of UI spec §1.3; the kit one stays reachable as KitStatusBadge.
export { StatusBadge } from "./booking/StatusBadge";
export { StatusBadge as KitStatusBadge } from "./core/primitives";
