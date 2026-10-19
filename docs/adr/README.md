# AA-Booking ADRs

Decisions that affect this app only. The cross-repo decisions behind AA-Booking (stack and bootstrap, supply
content ownership, per-app security groups, pricing and supplier rates, identity, the job model) are ecosystem
ADRs in the private AA-Ecosys repo under `docs/adr/` (0001 to 0008 and their amendments). An app ADR here
implements one of them and cites it. Infrastructure-only decisions live in AA-CIS-Infra.

Each ADR states its status near the top (`- **Status:** Accepted (dd/mm/yyyy, S<nnn>)`). Add every new ADR to
this table in the same PR.

| ADR | Decision | Status | Implements | Linear |
|---|---|---|---|---|
| [0001](0001-implements-root-0004.md) | Implement root ADR 0004 (stack and bootstrap) in this repo | Accepted (14/10/2026, S235) | root ADR 0004 | AA-678 |

Template: the ecosystem ADR template (AA-Ecosys `docs/templates/adr.md`).
