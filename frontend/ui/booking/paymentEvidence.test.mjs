// ui/booking/paymentEvidence.test.mjs — AA-994 S6d review: the payments row menu finds the evidence of a LIST row.
// Run: node --test ui/booking/*.test.mjs (Node 22 strips the TypeScript types of paymentEvidence.ts itself).
import assert from "node:assert/strict";
import { test } from "node:test";
import { paymentEvidenceFileId } from "./paymentEvidence.ts";

test("a list row (files: []) with evidence still offers the download", () => {
  assert.equal(paymentEvidenceFileId({ evidence_file_id: "f-evidence", files: [] }), "f-evidence");
});

test("the evidence id wins over the newest attached file", () => {
  assert.equal(
    paymentEvidenceFileId({ evidence_file_id: "f-1", files: [{ id: "f-1" }, { id: "f-2" }] }),
    "f-1",
  );
});

test("no evidence id: the newest attached file (detail-shaped row)", () => {
  assert.equal(paymentEvidenceFileId({ evidence_file_id: null, files: [{ id: "f-1" }, { id: "f-2" }] }), "f-2");
});

test("no evidence and no files: no download item", () => {
  assert.equal(paymentEvidenceFileId({ evidence_file_id: null, files: [] }), null);
  assert.equal(paymentEvidenceFileId({ evidence_file_id: null }), null);
});
