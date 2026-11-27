# AA-1190 — My work tile links and the price setup card (S296 follow-up, Booking part)

Live check S296: in CIS two tile targets opened unfiltered after a click (they read `window.location.search` once).
The CIS fix is in AA-CIS-App `docs/implementation-notes/AA-1190-tile-filters.md`.

## Decisions

- **Booking targets were already right.** Every Booking My work target reads its filters through `useUrlFilters`
  (`useSearchParams`); no staff page reads `window.location.search` (only the query provider builds a return URL).
  No page code changed; a test now keeps it that way.
- **Price setup card**: the count ("2 of 844 tours ready to buy") is its own line (semibold), the explanation a
  second line, instead of one run-on sentence without a full stop. The message keys are unchanged.

## Changed

- `frontend/app/(staff)/_parts/MyWorkBand.tsx`: two paragraphs, test ids `my-work-price-setup-ready` / `-help`.
- `frontend/e2e/layout/my-work.spec.ts`: the card lines; per tile with a filter, a click on the dashboard makes the
  same reads as a direct load of the tile's link (the target's own reads; the dashboard's are left out).
  `fixtures.ts` exports `MY_WORK`.

## Tradeoffs

- The click test compares reads, not screens: it holds for any filter translation (`owner=none` becomes
  `assignee=none` in the API) without a per-page assertion.

## Should know

- `npm run build` before `npm run test:layout` (the layout check measures the built `.next`).
