# ADR 0002 — Approval rules for a three-person team and business-owner self-approval

- **Status:** Proposed (S288), accepted when the AA-1158 part 1 PRs merge.
- **Implements:** root ADR 0006a A14 (the CIS business-owner marker, contract view `shared.v_staff_booking_grant`,
  CIS 462) and the tech lead decisions of S285 (Q4, Q5, Q9).
- **Linear:** AA-1158 (part 1; part 2 adds supplier bank accounts).

## Context

Booking applies maker != checker on every approval. The team that runs it has three people: the business owner, the
content + operations staff member (a custom role without any approval, AA-1155) and the tech lead. Strict maker !=
checker blocks the business owner's own work, while some gaps remained: a supplier made by a holder of
`supplier.activate` was active at once, an earlier contract writer could publish it, the bill approver could record
the payout, and one person could change the bank details AA prints on agency invoices. Reversing these rules touches
this repo only (CIS keeps the marker either way), so the ADR lives here.

## Decisions

1. **The business owner may approve her own work, flagged.** The identity whose CIS record carries
   `is_business_owner` approves her own supplier activation, contract publish, price version, season range (kind
   `season`) and holiday event, calendar default and plan calendar, supplier bill, agency payment, departure price
   adjustment and billing entity change. Each such row has `owner_self_approved = true` and its audit row says so.
   Never self-approved: Pricing > Policy changes (so she cannot switch these rules alone), stop-sales and every
   removal, rejections and withdrawals.
2. **The marker is checked twice, never taken from a request.** The API reads it uncached on every write
   (`custom_roles.is_business_owner`, fail closed: no answer means strict maker-checker); the database checks it again
   for any flagged row (`booking.owner_self_approval_guard()`, reading the contract view as role `booking`). Preview,
   Dev-only E2E and the E2E test user never self-approve.
3. **Suppliers start pending** whoever creates them; a second person activates them (`activated_by`), never its
   creator nor anyone who changed its data while it was not live, pending or inactive (`suppliers.writers`): a
   rejected or never-activated supplier holds data nobody approved.
4. **Every writer of a contract draft is a maker** (`writers`); none of them publishes it.
5. **The bill approver never records its payout**, except the business owner (Q4), flagged `same_approver_payer`;
   the bill's creator and facts editor never pay it (bk-030 rule, the business owner included). The tech lead is the
   fallback payer.
6. **The bank details and the default issuer are pending changes** a second person with `billing_config.approve`
   decides; the approval applies the change in the same transaction.
7. **`sellable` needs an approved price** (Q9): a matrix row still valid, computed or from a published version.
8. **Switches are policy values**, not constants: `owner_self_approval_allowed` and
   `owner_payout_after_approval_allowed` (Pricing > Policy, section security, approved by the administrator group).
9. **Oversight is visible to every auditor**: the audit filter `owner_self_approved=true` and the My work tile
   `owner_self_approvals` are shown to every `audit.read` holder, not only to the marker holder.

## Consequences

- Residual risk: a CIS owner can move the marker, including to themselves. Booking cannot prevent it; decision 9
  makes it visible. Proposed CIS follow-up: an owner cannot set the marker on their own account.
- MIG-4: the migration-only PR must keep the running code green, so two strict DB rules wait for part 2: refusing a
  supplier INSERT other than `pending` with a creator, and the guard on `billing_entity` itself. Until then the API is
  the control for those two.
- Part 2 plugs supplier bank accounts into the same pieces: the self-approval check, the generic DB guard, the
  pending-change pattern and the payout check list (a payout lock after a bank change, as a policy value).
