# AA-1007 (sidebar lane, S264): collapsible nav groups in the Booking admin sidebar

The tech lead asked for foldable sidebar groups in every app (S264). The behaviour is built once in the UI kit core
(AA-CIS-App `frontend/app/_kit/core`: `NavGroup.tsx`, `useNavGroups` in `useSidebar.ts`, the store at the end of
`sidebarStore.ts`; see that repo's `docs/implementation-notes/AA-1007-sidebar-groups.md`) and reaches Booking through
the Kit Sync bot. This repo only wires it.

## Decisions

- `ui/local/Sidebar.tsx` renders each group of `ui/local/nav.tsx` with the kit `NavGroup`, scope `booking`
  (localStorage `aa_nav_groups:booking`). Every Booking group keeps its title (Figma), so every group folds, the
  one-item Overview included (the logo still links to the Dashboard).
- The icon rail = collapsed, or the compact density window (`COMPACT_QUERY`, 768-1199 px, forced rail in
  `app/globals.css`), never the phone drawer. In the rail there is no group header and every item shows.
- The group of the current page is open on every arrival; other groups are folded until the user opens them.

## Changed

- `ui/core/*`: the kit core copy (Kit Sync; the first commit of the branch is a local try-out copy, replaced by the
  bot's `bot/kit-sync` PR before this PR merges).
- `ui/local/Sidebar.tsx`: groups through `NavGroup` + `useNavGroups`.
- `e2e/layout/sidebar-groups.spec.ts`: layout-mock check (1440 light/dark, 960 rail, 390 drawer light/dark) with
  screenshots.

## Tradeoffs

- In the compact window the first server render does not know the media query (server snapshot), so for a moment
  before hydration the folded groups' icons are missing from the rail; after hydration every item shows.

## Should know

- Renaming a group label in `nav.tsx` changes its id (slug of the label) and resets that group's saved fold.
