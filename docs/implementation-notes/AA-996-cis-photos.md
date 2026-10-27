# AA-996 fix slice 3 — CIS photos on catalog pages

Linear AA-996, Figma visual audit S254 (`AA-Ecosys/docs/audits/2026-10-28-S254-booking-figma-audit/`, items 2 and 3):
country cards, the country page and the geo drawers showed no CIS photos, Images read 0 for Vietnam, Japan and India,
and photo-less rows drew a broken-image glyph. Session S254, agent Claude Code (lane, orchestrated).

## Decisions

- **Images 0 for VN / JP / IN is the CIS data, not a Booking counting bug.** Diagnosis (read-only, from code and
  earlier audits, no DB access in this lane):
  - Every CIS photo is a `shared.place_photo` row synced from the AA Google Drive "CON" folders (AA-708). The sync
    list `CON_FOLDERS` in AA-CIS-App `services/photos/sync.py` has seven countries only: South Korea, Mongolia, China,
    Taiwan, Philippines, Bhutan, Sri Lanka. Vietnam, Japan and India have no Drive folder, so no matched photo exists
    for their tours or places.
  - `catalog.v_destination_photo` / `v_tour_photo` / `v_destination.photo_count` / `v_tour.cover_photo_path` all read
    the same `shared.v_*_photos` views (CIS 225 / 253 / 279 / 290), so every catalog photo column is empty there too.
  - Two independent readings agree: the S252 propagation audit ("tour photos = 0 in 8 / 14 countries, incl. India,
    Nepal, Japan, Laos, Thailand") and the TripPlanner home audit (8 / 14 destination tiles show the placeholder:
    India, Nepal, Japan, Laos, Philippines, Thailand, Pakistan, Vietnam).
  - The join keys of `COUNTRY_PHOTOS` are right: place photos join `v_destination` on `destination_id` and group by
    CIS's `country_iso2`; tour photos join `v_tour` on `(source_system, source_id)` (the key `v_tour_photo` carries,
    CIS 253), and `v_country` on the tour's country name (CIS 288: always a `v_country.name`).
  So the fix is (a) one real gap in the aggregate, (b) saying in the UI where photos come from, and (c) the cover and
  gallery the Figma frames ask for. More photos for VN / JP / IN = a CIS Drive folder for them (CIS work, not here).
- **One real counting gap fixed:** a tour photo tied to a place CIS could not resolve to a country (`country_iso2`
  NULL, "CIS places without a country") was counted nowhere: the place branch drops NULL countries and the tour branch
  took only place-less photos. It now counts for the tour's country (still once per path).
- **Country cover = the cover (position 1) of the country's best-covered place**, falling back to the first photo of
  its best-covered tour (place-less photos only). Ties break on the place id / source id, so the cover is stable
  between reads. Computed in the same query as the count (one scan of the photo views per request, as before).
- **Country gallery = `photo_items`, the first 24 distinct paths** in the same order (the cover first), each with a
  caption (place name, or the tour photo's place label, else the tour title) and its place id when it has one. The
  count stays the true total; the page says "first 24 of N".
- **Paths, not URLs, in the API** (`cover_photo_path`, `photo_items[].path`), like `v_destination.cover_photo_path`
  and the trip-plan responses: the frontend builds the URL with `cisPhotoUrl()` (CIS public API
  `/content/photos/<id>?size=…`, which 302s to the photos CDN). One URL pattern across the admin, no host in the API.
- **`CisPhoto` fallback is a neutral tile, never an icon:** a soft panel with an optional short label (country code,
  "No photo"), also used when the image fails to load (`onError`), so a missing or rejected photo never shows the
  browser's broken-image glyph. This alone fixes the trip-plan list thumbnails (audit item 2): the list already
  returns `cover_photo_path`; rows were the `ImageOff` glyph only because those tours have no CIS photo.

- **Review follow-up (same PR): `CisPhoto` gains `fit` and `onLoadError` props** instead of callers fighting its
  classes. `fit="contain"` replaces a caller's `object-contain` class, which never won: CisPhoto hard-coded
  `object-cover` on the same `<img>` and Tailwind emits `.object-cover` after `.object-contain`, so the lightbox
  cropped portrait photos to a middle band. `onLoadError` lets the country hero switch its caption to "CIS has no
  photo for <country> yet." when the cover fails, instead of naming a photo that is not on screen (failure remembered
  per path, like CisPhoto itself, so no effect and no stale state after a refetch).

## Changed

- Backend `app/catalog/geo.py`: `COUNTRY_PHOTOS` returns `cover_photo_path` next to `photos`; new
  `COUNTRY_PHOTO_ITEMS` for the detail; `list_countries` / `get_country` / `update_country` responses gain
  `cover_photo_path` (every country) and `photo_items` (detail). Additive: no field removed or renamed, no migration.
- Frontend:
  - `ui/booking/CisPhoto.tsx` is now a client component: no path, or a load error (`onError`, remembered per path so
    a new path is tried again), renders the neutral tile with an optional `fallbackLabel`. Every existing caller
    (trip-plan list / detail, destinations list, destination drawer) gets it without a change.
  - Country cards (`CountriesView.tsx`): cover on top (16:7) with an "N images" chip; the ISO code on the neutral tile
    when CIS has no photo.
  - Country page (`CountryDetailView.tsx` + new `CountryPhotos.tsx`): Overview opens with the cover hero (caption =
    the cover's place), and a "Photos · From CIS" gallery (first 24, "first 24 of N" when capped) whose tiles open a
    lightbox (`Modal`, previous / next buttons and arrow keys, "Open destination" for a place photo, which opens the
    destination drawer). "Open CIS photos ↗" (CIS `/admin/photos`) for CIS editor roles. Destinations and Tours tabs
    gain a cover thumbnail per row.
  - Lightbox photo: `fit="contain"` plus `min-h-[240px]`, so a tall photo shows whole (up to 70vh) and a photo that
    fails to load keeps a 240 px neutral tile under the previous / next buttons instead of collapsing to one text line.
  - Drawers (`GeoDrawers.tsx`): `PhotoStrip` (6 square thumbnails, "+N" on the last) in the country edit / add drawer
    and the destination edit drawer, under the read-only CIS fields.
  - Types in `lib/api/geo.ts` (`CountryItem.cover_photo_path`, `CountryPhoto`, `CountryDetail.photo_items`), layout
    fixtures (one country without photos, a 24-photo gallery).

## Tradeoffs

- **No tour-cover fallback from its places on the trip-plan list.** It would need `catalog.v_tour_place` per row, the
  view that parses itineraries (lesson S238: ~0.9 s per call with `ANY`). Tours without a photo show the neutral tile.
- **Cover rule is mechanical** (most photos wins), not curated: CIS has no "country hero" field. When CIS adds one,
  read it instead.
- Window counts in the photo union scan every photo row of every country for the list (same as the count before);
  the data is small (Dev: ~2.6 k matched rows). The window functions also stop Postgres from pushing `iso2 = $1` into
  the union for the one-country reads, so those scan the same rows; the NOT EXISTS on `v_destination` plans as one
  anti-join. Fine at this size; revisit with EXPLAIN if CIS photos grow by an order of magnitude.
- **Gallery is a capped list, not a paged endpoint.** 24 covers the Figma gallery; a paged
  `/countries/{iso}/photos` can come later if staff need every photo of a country here (they have CIS Photos).

## Should know

- Review fixes checked with a throwaway Playwright spec on the layout-mock build (not committed): portrait 300x900 photo
  in the lightbox renders `object-fit: contain` at 630 px (70vh of 900); with every image 404 the lightbox tile is
  240 px tall and the hero caption reads "CIS has no photo for Laos yet.". `CisPhoto` callers that pass no `fit` keep
  `object-cover`, unchanged.

- Live check after deploy (orchestrator): `GET /booking/catalog/geo/countries` — BT, LK, CN, KR, MN, TW, PH carry a
  `cover_photo_path` and `photos` > 0; VN, JP, IN show `photos: 0`, `cover_photo_path: null`. To confirm the
  diagnosis on Dev data (read-only, role booking): `SELECT t.country, count(*) FROM catalog.v_tour_photo tp JOIN
  catalog.v_tour t USING (source_system, source_id) GROUP BY 1` should have no Vietnam / Japan / India row.
- Not tested against a real PostgreSQL in this lane (no local test database): the integration test
  `tests/integration/test_bk029_geo_api.py` pins counts, covers and the gallery on the CI stub.

- Images 0 for Vietnam / Japan / India will stay 0 after this PR. The Figma cards then show the neutral tile with the
  ISO code; the page copy points at CIS Photos (Drive folders) as the source.
- Hotels grid / hotel drawer photo strip (audit item 14, also listed under slice 3 in the audit) are not in this
  slice: the lane brief scoped catalog countries / destinations / trip-plan thumbnails only.
