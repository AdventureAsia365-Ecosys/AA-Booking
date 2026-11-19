"use client";
// ui/local/useTx.ts — AA-1128 (B5): the React binding of the phrasebook (ui/local/phrasebook.ts). `const tx = useTx()`
// then `tx(STATUS_META[s].label)`: a module-level English label in the UI language (the provider's language, English
// outside a provider); any other string, null or undefined comes back as it is.

import { useCallback } from "react";
import { useLang } from "../core/useI18n";
import { phrase } from "./phrasebook";

export function useTx(): <S extends string | null | undefined>(text: S) => S {
  const lang = useLang();
  return useCallback(<S extends string | null | undefined>(text: S): S => phrase(text, lang), [lang]);
}
