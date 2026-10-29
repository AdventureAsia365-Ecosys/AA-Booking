"use client";
// ui/local/useTheme.ts
// The Booking staff admin's theme hooks (AA-683; AA-994 S4b): the kit core hooks (ui/core/useTheme.ts) bound to the
// Booking theme store (ui/local/theme.ts, key `bk_theme`). Used by the Topbar toggle and the Settings page.

import { createThemeHooks } from "../core/useTheme";
import { themeStore } from "./theme";

export const { useThemeChoice, useResolvedTheme, useChartColors } = createThemeHooks(themeStore);
