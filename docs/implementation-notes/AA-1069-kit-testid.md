# AA-1069 (lane kit-testid) — guard: kit buttons take testId, not a plain data-testid

Lane l26 of S277 (Claude Code). The fix is in the canonical kit core (AA-CIS-App `frontend/app/_kit/core/Buttons.tsx`):
`domProps()` used to set `data-testid` from `testId` after the native props, so a plain `data-testid` attribute on a
kit button was overwritten with undefined. It now keeps the native attribute when `testId` is unset. The Kit Sync bot
brings that core into `frontend/ui/core` with its own PR after the CIS change merges; this PR adds only the guard.

## Decisions

- Same AST scan as the CIS admin and CRM web guards, as a `node:test` file (`lib/kitButtonTestIds.test.mjs`), so
  `npm run test:unit` (CI Frontend Build, and the CIS Kit Consumers check) runs it. Booking has no vitest.
- Kit imports recognised: the `@/ui` barrel, `@/ui/core/...`, and relative paths into `ui` / `core`. A component
  imported from `@/ui/booking/...` is not a kit button.
- `ui/core` (the vendored kit copy) is not scanned: it is edited and guarded in AA-CIS-App.

## Changed

- New `frontend/lib/kitButtonTestIds.test.mjs` (family found, scanner self-test, whole-app scan).
- No call site change: the scan of `app/`, `ui/`, `lib/` finds no kit button with a plain `data-testid` today.

## Tradeoffs

- Only named imports from kit modules are followed; a local wrapper is not. The core fallback covers that case.

## Should know

- Independent of the CIS PR and of the kit sync PR: the scan reads `ui/core/Buttons.tsx` for the `domProps` family,
  which exists in the current copy.
