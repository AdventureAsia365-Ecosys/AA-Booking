# AA-1091 — A10e-3 synthetic supply (the pricing pair loads a synthetic supplier for the live check)

Lane aa1091sup of S280. Extends root ADR 0006a A10e-2 (the pricing pair, `AA-1091-e2e-pricing-pair.md`). Needs a
root ADR 0006a amendment "A10e-3" before merge (prepared by the lane, decided by the tech lead).

## Why

The S280 facts: the Booking workbench on Dev has 892 plans, 2 priced (one is Tour X, which the live check must not
touch), 890 `no_components`, and the component suggestions of 10 probed tours all answer `suggested 0 / unmatched > 0`.
The live check needs at least 3 tours whose components all have supplier rates, plus one tour whose suggestions are all
rated. Lesson S278: when a Done-when needs synthetic data, creating that data through the product is part of the
feature. Supplier rates come only from the supply routes, which the pricing pair could not call (403 `e2e_read_only`).

## Decisions

- **The maker loads the supply, the checker publishes it.** Same two identities as A10e-2: the maker
  (`e2e-cis-cs@`, Pricing team preset, Booking `contracting` = `supply.write`) creates the supplier, its hotels, room
  types and services, imports the rate sheet (a draft contract: the importer is its maker) and deactivates everything
  at cleanup; the checker publishes the draft (the contract maker-checker: the service refuses the draft's last writer,
  403 `maker_checker`, and the DB CHECK backs it). Both may expire it. No new identity, group or grant.
- **Synthetic supply = the maker's marked supplier.** A supplier is synthetic when its `created_by` staff row is the
  maker's address AND its name still starts with `SYNTHETIC` (a real person renaming it takes it out of reach). Its
  hotels, services and contracts follow their supplier. The create of the supplier itself has no record to look up:
  its body check (marked name, only `company_name`, `categories`, `country_code`, `destination_id`) is its whole check
  (`NEW_RECORD`).
- **Hotel and service creates are checked by the supplier their body names** (`BODY_SUPPLIER`, `supplier_id`, UUID or
  `SUP-` code), and their bodies carry a marked name and only the facts the matcher and the cost sheet read
  (destination, country, type, unit, the CIS `source_id` of a service). Never a contact, address, note, link, photo or
  free text: an E2E identity writes no personal or free-text data.
- **Contract status only, sent alone**: `{"status": "published" | "expired"}`. Terms are edited only through the rate
  sheet import (which writes a new draft); the guard refuses any other PATCH body.
- **Deactivations carry no reason** (empty body or `{}`), so no free text is written.
- **Codes and UUIDs.** Supply paths take either; the guard looks a code up only when it matches the kind's pattern
  (`SUP-` / `HTL-` / `SVC-` + 6 to 12 digits, upper-cased like `records.ref_filter`), anything else counts as real
  without a query. A contract is a UUID only.
- **Not allowed**: supplier edit / activate, contract create by form, rate create / edit / delete by form, hotel /
  service edit / activate, photos. The rate sheet is the one rate path (it is the customer-service team's documented
  path, "Import rate sheet").

## Changed

- `app/auth/e2e_staff.py`: nine supply writes on the maker, `CONTRACT_STATUS` on the checker; body checks for the
  creates, the contract status and the deactivations; `SUPPLIER_SYNTHETIC_SQL` and its hotel / service / contract and
  by-code forms (`CODE_SQL`); `BODY_SUPPLIER`, `NEW_RECORD`; `is_synthetic_record` split into `_lookup` (reference to
  SQL + key) and `_fetch_synthetic`; `reads_body` also reads `BODY_SUPPLIER` writes; `check_synthetic` passes
  `NEW_RECORD`.
- Tests: `tests/unit/test_aa1091_e2e_synthetic_supply.py` (bodies, lookups, the real app: before-DB refusals,
  refusals before the staff sync, allowed writes reach the route, the checker's single supply write, a real
  contracting member unaffected), `tests/integration/test_aa1091_e2e_synthetic_supply.py` (the SQL on PostgreSQL); the
  AA-1087 reviewed-writes test and the "checked exactly one way" partition test list the new writes and ways; the
  A10e-2 test's maker / checker overlap now includes the contract status.

## Tradeoffs

- **Supply is catalogue-wide while active.** A synthetic hotel at a CIS destination is a suggestion candidate for every
  tour that overnights there, and its services for tours of its country (a guide needs only the country). Accepted for
  the few hours of the run; the cleanup deactivates every synthetic record (suggestions read active records only).
- **CIS supply content drafts.** Every new hotel / service emits a `supply` outbox event; CIS queues one
  `supply_content_draft` LLM job per applied facts version (CIS `services/supply_content/pull.py`; capped at 20 paid
  writer calls a day and $1.00 a day by its cost guard, never published without a human). The runbook keeps the entity
  count small and names this cost for the tech lead; the job skips an entity already inactive.
- Deactivating a synthetic item that a real plan uses as a component would make that plan `inactive_component`. Not
  guarded (a real staff member adding a `SYNTHETIC` item to a real tour within the run window is unlikely on Dev); the
  runbook's fact step counts plans using the synthetic items before the cleanup.

## Should know

- Removed with the A10 family (AA-1054), together with A10e-2.
- No threshold, limit or cap was added. The code pattern length (6 to 12 digits) is a format check, not a policy.
- Merge needs an auth-risk review (subagent or kiro-reviewer) and the root ADR 0006a A10e-3 amendment.
