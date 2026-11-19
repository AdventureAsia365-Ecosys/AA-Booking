# AA-1128 follow-up: Vietnamese leftovers of the staff admin (S285)

A live check of the staff admin in Vietnamese (`bk_lang=vi`) after the three Vietnamese PRs (notes `AA-1128-vi.md`)
still found English text: module-level option lists and enum maps rendered without the phrasebook, and two sentences
glued to their link. This slice fixes those with the same pattern (kit catalogues + phrasebook + `tx()`), frontend only.

## Decisions

- **Same pattern, no new mechanism.** Module-level English labels stay English in code and are shown with `tx()` /
  `<Phrase>` where they are rendered; their Vietnamese is a phrasebook catalogue. Two new catalogues, registered in
  `ui/local/phrases/shell.ts`: `ui/booking/roles.messages.ts` (Booking roles + CIS roles) and
  `ui/booking/channels.messages.ts` (pricing channels).
- **Channel labels worded as on Pricing rules & FX.** The rules page keeps its own code-keyed catalogue
  (`channel_b2c_tailor_made`, ...); the new phrasebook catalogue repeats its Vietnamese words, and the unit test fails
  when the two drift apart.
- **CIS role names.** The `cis_role` column showed the raw enum (`customer_success`). `lib/rbac.ts` gains
  `CIS_ROLE_LABEL` / `cisRoleLabel()`, named as the CIS admin names its roles (CIS `lib/rbac.messages.ts`, both
  languages); `reviewer` is the old alias of `content`; an unknown role shows as it is.
- **Hotel style is free text, not an option list.** `hotel_style` is typed by contracting (up to 60 characters); the
  API has no list. The usual styles (`HOTEL_STYLES` in `app/(staff)/_parts/supplyLabels.ts`) have a Vietnamese name;
  `hotelStyleText()` maps a case-insensitive match to the registered spelling and returns anything else as typed. Only
  display goes through it: the edit form keeps the stored value (`txDisplayOnly` rule).
- **Import-free label modules.** `SERVICE_TYPE_LABEL`, `UNIT_LABEL`, `SERVICE_UNIT_OPTIONS`, the star labels and the
  hotel styles moved to `app/(staff)/_parts/supplyLabels.ts` (re-exported from `supply.tsx`), and `CHANNELS` to
  `lib/api/channels.ts` (re-exported from `lib/api/proposals.ts`), so a node unit test can load them. Call sites
  did not change.
- **One test for every option list.** `ui/booking/viOptionLabels.test.mjs` loads the label modules and catalogues
  through a small resolve hook (maps `@/` and extension-less relative imports to `.ts`) and checks each label of the
  role, channel, service type, unit, star and hotel style lists resolves in Vietnamese. Loanwords kept as they are
  ("B2B", "Homestay", "Hostel") must still be registered.
- **Glued sentence + link, everywhere.** JSX drops the line break between `{t("… approved.")}` and a `<Link>` on the next
  line. Besides the two found live (calculator FX notice, settings quote validity) the same shape was in the hotel
  drawer ("via"), the readiness banner, the case checks tab (two), the trip plan commercial drawer and the country
  detail "Showing the first". All get `{" "}`; the unit test now refuses the shape in `app/`, `ui/booking`, `ui/local`.

## Changed

- Dashboard recent cases (table and phone cards): "Unassigned" in the UI language; `salesOf()` returns null instead
  of the English word.
- Case detail "Trip details": status, duration ("6 days", "1 day"), "N pax" and "Manual" through `tx()`.
- Hotels: search placeholder through the page catalogue; star options and the star tooltip via `starsLabel()`
  ("1 star" no longer reads "1 stars"); hotel style in the table, card and drawer; "Rates in force" tooltip
  (`RatesInForce` cell shared with services).
- Services: type and unit in the table; rate drawer "Net is per person." with the unit translated.
- Pricing channels: calculator "Rule in force (…)", proposal locked price, trip plan detail and price matrix rows.
- Staff: Booking role chips, CIS role column, staff detail and settings CIS role, the activate dialog and the role
  drawer sentence, the staff picker suffix.
- /cases: the row button's accessible name "Open TC-…" / "Mở TC-…".
- Trip check types (`TRIP_CHECK_CODE_LABEL`): "Re-check bookings" and "Insurance" had no catalogue entry, so the
  /trip-checks title column, its check-type filter, the check drawer title and the case Checks tab showed them in
  English. Both are now in `trip-checks/_parts/CheckDrawer.messages.ts` (next to "Emergency contact"), and the unit
  test checks every value of the map (review finding, fix pass).
- Other enum maps that bypassed the phrasebook: trip check title + "Standard"/"Custom" on /trip-checks, finance
  departures cost basis, readiness status filter options, the notification type fallback title (page + popover).

## Tradeoffs

- The hotel style map translates only exact (case-insensitive) matches; "Boutique riverside" stays as typed. A real
  option list would need an API change (a `hotel_style` enum), which is out of scope for a UI slice.
- Two wordings of one English key across catalogues still resolve by the phrasebook's "sorts first" rule; the new
  catalogues reuse the existing words ("Quản trị", "Bán hàng", "Khách sạn") so nothing changes elsewhere.

## Should know

- "Margin" stays as is (loanword) and country names stay data, as asked.
- CSV export headers stay English (files for other tools).
- No `ui/core/**` file is edited.
