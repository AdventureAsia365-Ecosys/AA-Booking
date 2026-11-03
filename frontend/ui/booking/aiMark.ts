// ui/booking/aiMark.ts — AA-1040 (EU AI Act Art. 50): the AI mark of the CIS supply text Booking shows (hotel and
// service descriptions drafted by the CIS writer stage supply_content_writer, reviewed and published in CIS; CIS 366
// serves the mark on catalog.v_supply_content). Pure helpers, tested by aiMark.test.mjs.

/** The machine-readable mark of CIS text (the API's `content.ai` / `supply.ai`). `ai_generated` false = a person wrote
 *  it from scratch in CIS; the other fields are then null. `human_edited` = staff edited the AI draft before it was
 *  published. `model` stays null (CIS records it on its own ledger). */
export interface AiMark {
  ai_generated: boolean;
  generator: string | null;
  model: string | null;
  generated_at: string | null;
  human_edited: boolean | null;
}

/** Whether CIS text shown to staff gets the kit "AI-assisted" badge: every published supply text starts as a draft of
 *  the CIS writer, so the badge shows unless CIS says a person wrote it from scratch (`ai_generated: false`). A null
 *  mark (CIS 366 not served yet) keeps the badge. No text, no badge. */
export function showsAiBadge(text: string | null | undefined, ai: AiMark | null | undefined): boolean {
  return Boolean(text) && ai?.ai_generated !== false;
}

/** The machine-readable mark on one block of AI-written CIS text (Art. 50(2)); never page-wide, since prices, facts
 *  and photos on the same page are not AI output. `{}` when the text is not AI-written. */
export function aiTextAttrs(text: string | null | undefined, ai: AiMark | null | undefined): Record<string, string> {
  if (!showsAiBadge(text, ai)) return {};
  const attrs: Record<string, string> = { "data-ai-generated": "true", "data-ai-source": "cis-supply-content" };
  if (ai?.generated_at) attrs["data-ai-generated-at"] = ai.generated_at;
  if (ai?.human_edited != null) attrs["data-ai-human-edited"] = ai.human_edited ? "true" : "false";
  return attrs;
}
