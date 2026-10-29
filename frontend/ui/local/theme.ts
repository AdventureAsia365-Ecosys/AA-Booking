// ui/local/theme.ts
// The Booking staff admin's theme store (AA-683; AA-994 S4b, root ADR 0016 decision 4): the kit core store
// (ui/core/theme.ts) bound to the Booking key `bk_theme` (UI spec §1.1: Booking keeps its own key). app/layout.tsx puts
// `themeStore.restoreScript` in its <head>; ui/local/useTheme.ts binds the hooks.
// No "use client": the server root layout reads the restore script from here.

import { createThemeStore } from "../core/theme";

export type { ResolvedTheme, ThemeChoice } from "../core/theme";

export const THEME_KEY = "bk_theme";

export const themeStore = createThemeStore({ storageKey: THEME_KEY });
