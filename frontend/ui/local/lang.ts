// ui/local/lang.ts — AA-1091: the Booking staff app's UI language store (kit core/i18n.ts; English by default, cookie
// `bk_lang`). Pages opt in one at a time: a page that shows the kit LangSwitch wraps itself in the kit I18nProvider
// with this store, so the rest of the app stays English until its pages have Vietnamese messages too (no page ever
// mixes the two languages).
import { createLangStore } from "../core/i18n";

export const langStore = createLangStore({ cookieName: "bk_lang" });
