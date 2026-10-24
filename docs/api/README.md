---
id: APP-05-booking
title: "AA-Booking API reference and breaking-change check"
description: Where the generated OpenAPI document of the Booking API lives, how to regenerate it, what the breaking-change check fails on and how to mark a PR as an intended breaking change.
type: how-to
status: draft
owner: nghiep
audience: [engineer, agent]
applies_to: [aa-booking-api]
last_reviewed: 2026-10-24
review_every_days: 90
related: [APP-05, APP-07, ARCH-07]
---

# AA-Booking API reference and breaking-change check

`docs/api/openapi.json` is the OpenAPI document of the Booking API, generated from the code (AA-899). It is the API
reference of this repo and the base of the breaking-change check. Never edit it by hand.

## Regenerate it

After any change to a route, a request or response model, or the FastAPI / Pydantic pins:

```bash
cd backend && python3 scripts/export_openapi.py
```

Commit the result in the same PR. The script needs no database, network or secret: it builds the app with
`ENVIRONMENT=test` settings and calls `app.openapi()`. The output is deterministic (sorted keys, no `servers`,
`info.version` = `unreleased`), so the same code always gives the same bytes. Use the pinned FastAPI and Pydantic from
`backend/requirements.txt`: another version can change the output (the check prints the versions it ran with).

## The two checks

| Check | Where | Fails when |
|---|---|---|
| Stale document | `Unit Tests` (required), `tests/unit/test_openapi_export.py` | the committed file differs from what the code generates |
| Breaking change | `API contract` / `OpenAPI breaking changes` (`.github/workflows/api-contract.yml`) | oasdiff finds an error-level change on a **public** path between the base branch's file and the PR's file |

**Public paths** are the ones a client outside this repo calls: `/booking/b2b/v1/*` (AA-Partner, contract
`booking.b2b.v1`), `/booking/health*` (ALB, deploy smoke test) and `/booking/ops/*` (the Deploy Dev workflow). Staff
routes are called only by this repo's staff admin (`frontend/`), which changes in the same PR: their breaking changes
are listed as warnings and in the job summary, never failing. Every change, breaking or not, is listed in the job
summary (oasdiff changelog).

**What counts as breaking** is oasdiff's error level (pinned version in the workflow), with two checks raised to error
in `.github/oasdiff/severity-levels.txt` (`response-optional-property-removed`, `request-parameter-removed`). In
short: a removed path or operation, a removed response field (required or optional), a new required request
parameter or field, a removed request parameter, a narrowed type, enum or limit on a request, a widened response
enum, a response field that became optional or nullable, a removed success status or media type. Adding an optional
field, a new path or a new operation is not breaking.

A response can only be checked field by field when the route declares its model (`response_model=`, a return type
that is a Pydantic model, or `responses={code: {"model": ...}}` for a hand-made `JSONResponse`). A route that returns a
bare `dict` is a hole in the check: give public routes a model.

## Making a breaking change on purpose

Prefer an additive change (keep the old field, add the new one, deprecate, remove later; handbook §5.6). When the
break is intended, mark the PR in either way:

- the label **`api-breaking`** (adding or removing the label re-runs the check by itself), or
- a conventional breaking title, `feat!: ...` / `fix!: ...` (release-please then records it as a breaking change).

The check then passes with a warning per breaking change. Put the client plan in the PR body: who calls the path,
what they change and when (for `booking.b2b.v1`, a new contract major per root `docs/contracts/README.md`).

## Releases

Each GitHub release carries `aa-booking-openapi-vX.Y.Z.json`: the document of the released commit with
`info.version` set to the release version (`.github/workflows/release-please.yml`).

## Local run of the breaking check

```bash
git show origin/main:docs/api/openapi.json > /tmp/base-openapi.json
BASE_SPEC=/tmp/base-openapi.json HEAD_SPEC=docs/api/openapi.json PUBLIC_PATHS='^/booking/(b2b/|health|ops/)' SEVERITY_LEVELS=.github/oasdiff/severity-levels.txt bash .github/scripts/openapi_breaking.sh
```

It needs `oasdiff` on the PATH (the version and checksum the workflow pins).
`.github/scripts/openapi_breaking.sh` is a byte copy of the AA-CIS-App script: change both together.
