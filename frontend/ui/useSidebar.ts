// AA-683: copied from AA-CIS-App frontend/app/_kit/useSidebar.ts at CIS commit 039bb250 (ADR 0004 decision 1).
// Keep byte-comparable with CIS: change only imports and storage keys here; brand changes go to CIS first.
"use client";
// app/_kit/useSidebar.ts
// AA-752 — React binding for the sidebar collapsed-state store (app/_kit/sidebarStore.ts).
// useSyncExternalStore, so it is React-Compiler safe (no setState-in-effect) and reads the stored
// value without a flash (same pattern as useThemeChoice).

import { useSyncExternalStore } from "react";
import {
  getCollapsedSnapshot,
  getServerCollapsedSnapshot,
  setCollapsed,
  subscribeSidebar,
} from "./sidebarStore";

export function useSidebarCollapsed(): [boolean, (c: boolean) => void] {
  const collapsed = useSyncExternalStore(
    subscribeSidebar,
    getCollapsedSnapshot,
    getServerCollapsedSnapshot,
  );
  return [collapsed, setCollapsed];
}
