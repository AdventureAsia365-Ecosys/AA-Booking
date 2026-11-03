// ui/booking/aiMark.test.mjs — AA-1040: the "AI-assisted" badge and the machine mark on CIS supply text.
// Run: node --test ui/booking/*.test.mjs (Node 22 strips the TypeScript types of aiMark.ts itself).
import assert from "node:assert/strict";
import { test } from "node:test";
import { aiTextAttrs, showsAiBadge } from "./aiMark.ts";

const DRAFT = { ai_generated: true, generator: "Adventure Asia supply content writer", model: null,
  generated_at: "2026-10-02T03:00:00+00:00", human_edited: false };
const OWN = { ai_generated: false, generator: null, model: null, generated_at: null, human_edited: null };

test("badge on AI text, and on CIS text whose mark is not served yet", () => {
  assert.equal(showsAiBadge("A quiet garden hotel.", DRAFT), true);
  assert.equal(showsAiBadge("A quiet garden hotel.", { ...DRAFT, human_edited: true }), true);
  assert.equal(showsAiBadge("A quiet garden hotel.", null), true);
  assert.equal(showsAiBadge("A quiet garden hotel.", undefined), true);
});

test("no badge without text or for a text a person wrote from scratch", () => {
  assert.equal(showsAiBadge(null, DRAFT), false);
  assert.equal(showsAiBadge("", DRAFT), false);
  assert.equal(showsAiBadge("Written by our team.", OWN), false);
});

test("the machine mark carries the generation time and the edit flag", () => {
  assert.deepEqual(aiTextAttrs("A quiet garden hotel.", { ...DRAFT, human_edited: true }), {
    "data-ai-generated": "true", "data-ai-source": "cis-supply-content",
    "data-ai-generated-at": "2026-10-02T03:00:00+00:00", "data-ai-human-edited": "true",
  });
  assert.deepEqual(aiTextAttrs("A quiet garden hotel.", null), {
    "data-ai-generated": "true", "data-ai-source": "cis-supply-content",
  });
  assert.deepEqual(aiTextAttrs("Written by our team.", OWN), {});
});
