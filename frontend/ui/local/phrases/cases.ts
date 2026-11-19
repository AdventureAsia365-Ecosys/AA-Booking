// ui/local/phrases/cases.ts — AA-1128 (B5): registers the trip case, proposal, B2B desk, departure, trip check and agency billing message catalogues in the phrasebook
// (ui/local/phrasebook.ts). The staff shell imports ui/local/phrases (index.ts) once, so the server and the browser
// hold the SAME catalogues before the first render: `tx()` then gives the same text on both sides (no hydration
// mismatch), whichever pages the server rendered before. One file per page group, so the PRs of the groups never
// edit the same lines; ui/booking/phrasebookRegistry.test.mjs checks that every catalogue is listed once.
export {};
