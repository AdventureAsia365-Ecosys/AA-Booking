<!--
Title: "<type>: <what changed>" in English. Types: feat, fix, chore, refactor, docs, ci, test, perf.
The "PR title" check enforces this. No "[AA-123]" in the title. In the body write "Refs AA-123";
never Fixes/Closes/Resolves (Linear would close the issue).
This repo is public: no account IDs, ARNs, SG/VPC IDs, secret values or customer data in the PR or the diff.
Security and infrastructure findings go to the private ops docs, not here.
-->

## What

-

## Why

Refs AA-

## Risk and rollback

- Risk: low / medium / high (high: customer or payment data, auth, migrations, deletes, pricing, IAM)
- Blast radius: API / staff admin / migration / CIS contract read:
- Rollback: revert and redeploy the previous image, or:

## Cost

- [ ] 0 USD: no new AWS resource and no new paid API
- [ ] Adds spend: monthly estimate:

## Docs checklist

- [ ] `CONTEXT.md` updated (boundary, stack, ownership, endpoints or migrations list changed), or N/A
- [ ] ADR added or amended, and listed in `docs/adr/README.md`; cross-repo decisions go to the root ADRs; or N/A
- [ ] Runbook or how-to updated (an ops procedure changed), or N/A
- [ ] Contract or API shape change noted (contract version, CHANGELOG), or N/A
- [ ] Implementation note `docs/implementation-notes/AA-xxx.md`

## Verification

- [ ] CI green (Lint, Security Audit, Unit Tests, Integration Tests, Docker Build Check; Vendored CIS copies and Frontend Build when touched)
- [ ] Migration `bk-NNN`: identical copy in AA-CIS-Infra `db/migrations/booking/`, applied on Dev by DB Migrate before this merges; or N/A
- [ ] Staff admin change: Vercel preview checked on desktop and mobile; or N/A
- [ ] After deploy: `GET /booking/health` and `/booking/health/ready` return 200, and the changed endpoint is verified live (status + shape)
- Evidence (commands, output, screenshots):
