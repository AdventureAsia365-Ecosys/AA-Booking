"use client";
// ui/local/useBookingFormat.ts — AA-1128 (vi): the Booking text formatters bound to the current UI language, so a page
// shows days, months, numbers and amounts in the same format as the kit <DateText> / <Money> next to them ("9 Oct
// 2026" / "9 thg 10, 2026", "1,845.00 USD" / "1.845,00 USD"). In a component:
//   const { formatDay, formatMoney } = useBookingFormat();
// The names shadow the plain helpers (ui/booking/dates.ts, kit core/format.ts), which take the language as their last
// argument and stay English by default; module-level code that cannot call a hook takes `lang` as a parameter.

import { useMemo } from "react";
import { formatMoney } from "../core/format";
import { useLang } from "../core/useI18n";
import { formatDay, formatLongDay, formatMonth, formatNumber, localizeIsoDates } from "../booking/dates";

export interface BookingFormat {
  lang: "en" | "vi";
  formatDay: (value: string | null | undefined) => string;
  formatLongDay: (value: Date | null | undefined) => string | null;
  formatMonth: (year: number, month: number, style?: "long" | "short") => string;
  formatNumber: (value: number | string | null | undefined, maxFractionDigits?: number) => string;
  formatMoney: (amount: string | number | null | undefined, currency: string | null | undefined) => string;
  localizeIsoDates: (text: string) => string;
}

export function useBookingFormat(): BookingFormat {
  const lang = useLang();
  return useMemo<BookingFormat>(
    () => ({
      lang,
      formatDay: (value) => formatDay(value, lang),
      formatLongDay: (value) => formatLongDay(value, lang),
      formatMonth: (year, month, style = "long") => formatMonth(year, month, lang, style),
      formatNumber: (value, maxFractionDigits = 2) => formatNumber(value, lang, maxFractionDigits),
      formatMoney: (amount, currency) => formatMoney(amount, currency, lang),
      localizeIsoDates: (text) => localizeIsoDates(text, lang),
    }),
    [lang],
  );
}
