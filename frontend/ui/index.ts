// ui/index.ts
// AA-683 — barrel for the Booking UI: the copied CIS kit + the Booking shell + Booking components
// (ui/booking/*). Pages import from "@/ui".

export * from "./tokens";
export * from "./brand";
export * from "./primitives";
export * from "./Modal";
export * from "./Drawer";
export * from "./Toast";
export * from "./DataTable";
export * from "./csv";
export * from "./savedViews";
export { Providers } from "./Providers";

export { AdminShell } from "./AdminShell";
export { Sidebar } from "./Sidebar";
export { Topbar } from "./Topbar";
export { BottomNav } from "./BottomNav";
export { CommandPalette } from "./CommandPalette";
export * from "./nav";
export * from "./StaffContext";
export { useSidebarCollapsed } from "./useSidebar";
export { useMediaQuery, useIsPhone, PHONE_QUERY } from "./useMediaQuery";
export { useThemeChoice, useResolvedTheme } from "./useTheme";

// Booking components (UI spec §5)
export * from "./booking/StatusBadge";
export * from "./booking/Stepper";
export * from "./booking/ConfirmDialog";
export * from "./booking/FormDrawer";
export * from "./booking/FilterBar";
export * from "./booking/KpiTile";
export * from "./booking/Section";
export * from "./booking/PageFrame";
export * from "./booking/WaveFrame";
export * from "./booking/Money";
export * from "./booking/DateText";
export * from "./booking/FromCis";
export * from "./booking/CisPhoto";
export * from "./booking/PersonCell";
export * from "./booking/Code";
export * from "./booking/NotificationPopover";
export * from "./booking/notifications";
export * from "./booking/NotificationItem";
export * from "./booking/audit";

// Kit loading placeholders under the spec names (UI spec §5 "Skeleton").
export { SkeletonBar as Skeleton } from "./primitives";

// Name clash: the kit StatusBadge maps job-runner states (CIS). In Booking, `StatusBadge` is the
// per-entity map of UI spec §1.3; the kit one stays reachable as KitStatusBadge.
export { StatusBadge } from "./booking/StatusBadge";
export { StatusBadge as KitStatusBadge } from "./primitives";
