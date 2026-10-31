# Changelog

## [0.15.0](https://github.com/AdventureAsia365-Ecosys/AA-Booking/compare/v0.14.0...v0.15.0) (2026-10-31)


### Features

* mark CIS places hidden from customers on Booking destination screens ([#160](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/160)) ([636df62](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/636df62c8286910004e6783c702e470e7ddec91e))
* proposal items read back today's CIS text; B2B confirm names what was sold; preview fits 390 px ([#163](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/163)) ([16fbed6](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/16fbed68bcb7995d6635a24adce62d7c6b855a98))

## [0.14.0](https://github.com/AdventureAsia365-Ecosys/AA-Booking/compare/v0.13.0...v0.14.0) (2026-10-30)


### Features

* hold answers carry the held quote's hash, FX lock and line prices (booking.b2b.v1 1.2) ([#155](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/155)) ([5f2b320](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/5f2b3203ab728aa16d3766eedf36ed45f0d75514))
* hotel photo grid, billing row menus, pages on the kit frame and form controls ([#156](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/156)) ([45de036](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/45de0368663b2c70a6156c8f456d152398968c26))


### Bug Fixes

* schema gate accepts a newer green standalone isolation probe after a probe-only red run ([#153](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/153)) ([e2412c6](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/e2412c6d4e46c04b384bb8855bf13c4c456352c6))

## [0.13.0](https://github.com/AdventureAsia365-Ecosys/AA-Booking/compare/v0.12.0...v0.13.0) (2026-10-30)


### Features

* bk-034 expand index on trip_case_version_itinerary.source_tour_id ([#149](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/149)) ([f6604ac](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/f6604ac665559595ae0befe0ca30493afe64438e))
* inquiry inbox key contract view for the CIS TP-03 handoff check (bk-035) ([#152](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/152)) ([22d55dd](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/22d55dd8cb0224d66ab8d6f3b4321ea73a98f902))

## [0.12.0](https://github.com/AdventureAsia365-Ecosys/AA-Booking/compare/v0.11.0...v0.12.0) (2026-10-29)


### Features

* Deploy Dev schema-gate and CI Migration preflight ([#146](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/146)) ([541d303](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/541d3035b8918bbe1e680bddc2de8f582642c343))

## [0.11.0](https://github.com/AdventureAsia365-Ecosys/AA-Booking/compare/v0.10.0...v0.11.0) (2026-10-29)


### Features

* registered_keys on /booking/health/ready ([#139](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/139)) ([82e36d5](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/82e36d5bf1a486e9f207e7191a78b4c3a6aee094))


### Bug Fixes

* ops-docs uploads use SSE-S3; readiness reports bucket configuration ([#142](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/142)) ([5a11502](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/5a1150233525d5a98e54dbf1aad6c76927a633a9))
* read the trip-plans list destinations from catalog.v_tour_place_name (CIS 298) ([#141](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/141)) ([ad4be19](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/ad4be1946c5b59ea4227b521e06a45672f1a66b0))
* restore bk-032 to its applied bytes and widen effective_margin in bk-033 ([#137](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/137)) ([dfea34c](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/dfea34c24bb6ca0c11847ee2376cd414755fb4a8))

## [0.10.0](https://github.com/AdventureAsia365-Ecosys/AA-Booking/compare/v0.9.0...v0.10.0) (2026-10-28)


### Features

* catalog read perf guard (plan check, no N+1, slow query log) ([#132](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/132)) ([913b377](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/913b377b0391678484df99a2e8f7df15fd515d5d))


### Bug Fixes

* bound the trip-plans list destinations read (chunks, page budget, cache) ([#134](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/134)) ([f6e997b](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/f6e997b8ef83661e3aed43fe4222e70ff5ffc3c6))
* remove internal issue IDs from user-visible text ([#136](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/136)) ([39a15d2](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/39a15d27066e4868b413e3d68d7bfd82f2f59537))

## [0.9.0](https://github.com/AdventureAsia365-Ecosys/AA-Booking/compare/v0.8.0...v0.9.0) (2026-10-28)


### Features

* billing detail, notifications and staff fixes from the Figma audit (AA-996 slice 5) ([#128](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/128)) ([b5b2f8f](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/b5b2f8f42206b7741c59349cf25a4c49f6a328cd))
* bk-029 country and destination profiles (Booking state of CIS places) ([#115](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/115)) ([67a2f31](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/67a2f3144b7e1ed125895e96a37eebff72e1f282))
* bk-030 supplier bills, lines and payouts (migration) ([#119](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/119)) ([eb38a78](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/eb38a789f9120c8f5cc32a8143a67d83c3b2f912))
* bk-031 adds the missing CIS tour countries to booking.countries ([#123](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/123)) ([bfeabce](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/bfeabceba1aa717b858d3bd4fe17db238fbf5a4f))
* bk-032 trip plan supplier and price matrix table ([#129](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/129)) ([2294369](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/2294369b7ba0dbb739c0053c738e73e0c21e49cb))
* Booking covers every Figma screen (coverage matrix, sign-in & security, soft-delete dialogs, CIS read-only forms, Destinations and Countries) ([#117](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/117)) ([c606f60](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/c606f60cc0416183ea0dff43e693521352da44c5))
* case workspace and proposals per the Figma audit (AA-996 slice 4) ([#127](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/127)) ([de3be3c](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/de3be3c3283f0412944f9821e8b752b579ad07f8))
* CIS photos on catalog countries (cover, gallery, drawer strips) and a neutral photo fallback ([#125](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/125)) ([26b8a8a](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/26b8a8abfb2ffe487dc52cef116a1555fd677b69))
* compact density for staff lists on zoomed laptops ([#124](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/124)) ([96c1bcf](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/96c1bcf44d04cf8961d6c0b0509219ac26fa470f))
* deploy gate blocks only on running retention jobs, plus a Dev-only bk_noop_sleep test kind (AA-993 G5) ([#121](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/121)) ([5aa2e69](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/5aa2e69a2ca1d0ff5d28b5a2af51be338fc4ac65))
* en-GB date fields, Figma filter bar and CSV on every staff list ([#126](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/126)) ([7473755](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/7473755c0cc9ca7d7dab2332c2185acfb33c50ed))
* My Payments customer routes for signed-in travellers (booking.customer.v1) ([#122](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/122)) ([d654e39](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/d654e398c198a7b26b4e74f5f84e7c3d3d5b8616))
* supplier bills and payouts (AA-682 S4, bk-030) ([#120](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/120)) ([0c54476](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/0c54476570b0ed05486393bb6172b3e44318b849))
* trip plan supplier and price matrix, sellable-first list, plan picker (AA-996 slice 2) ([#130](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/130)) ([d78ae20](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/d78ae200a89bb9f2c3a6130e2ed371398a9166ec))


### Bug Fixes

* Booking countries from CIS ISO codes, images from CIS photos ([#118](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/118)) ([7114271](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/711427102308f2a323402d572c1cf88da2b1cb03))
* trip-plans list 503 from per-row catalog day reads; extras fail soft ([#131](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/131)) ([81fd84c](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/81fd84c2551f988016e53972ed550ddc6e36c7d8))

## [0.8.0](https://github.com/AdventureAsia365-Ecosys/AA-Booking/compare/v0.7.0...v0.8.0) (2026-10-27)


### Features

* bk-025 count-only ops views for the CIS data coverage page ([#94](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/94)) ([172ffc7](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/172ffc7b22c4f774e1e4977e22b75fea99ffca62))
* bk-026 supplier bookings, ops files and the post-sale case guard (migration only) ([#99](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/99)) ([7880edb](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/7880edb9b1a6b2c4263511eed65b28e8e904a27d))
* bk-027 trip checks, seeding trigger and check board (migration) ([#106](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/106)) ([3a74ac0](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/3a74ac0aaa03c15438cba0d887c5e761c509ca98))
* bk-028 invoices, payment schedule and manual payments (migration) ([#111](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/111)) ([75f200b](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/75f200befedd58d8203ac064138fdacd10633736))
* invoices, payment schedule and manual payments: API, reminders, staff UI ([#114](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/114)) ([ac540fc](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/ac540fc9b758fa72680330379179464f2f74c6c9))
* supplier bookings (AA-682 S1): API, case supply track, B2B desk tie-in, staff UI ([#100](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/100)) ([f6526f4](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/f6526f40676d52461d9de5e7ac2e5811f2348f8f))
* trip checks and case lifecycle: API, daily jobs, staff UI ([#107](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/107)) ([988e0a6](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/988e0a66f7d3a8f8f40fe08d340d7ba688df0c48))


### Bug Fixes

* B2B quote leaves out the live hold of its own proposal version ([#97](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/97)) ([5569f2b](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/5569f2bb4eec46b5ac181cb6a7b80502671ba17c))
* bk-028 amendment window instead of deferred sums triggers (migration) ([#113](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/113)) ([10fd7ee](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/10fd7eef7cbed936c6beb683ee514abbd9311cd8))
* run the production smoke on ubuntu-24.04 ([#108](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/108)) ([740ac71](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/740ac714115a28922a274528a196f44bf4e8baec))
* trip checks board keeps the due date on one line ([#109](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/109)) ([c0a8095](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/c0a80957b65be9ec6a366004843c00c6f6c784d5))

## [0.7.0](https://github.com/AdventureAsia365-Ecosys/AA-Booking/compare/v0.6.0...v0.7.0) (2026-10-25)


### Features

* Dev manual override run for the Booking retention kinds ([#91](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/91)) ([8d00c76](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/8d00c76d769087972cccbabb9591f88c3fbb5be1))


### Bug Fixes

* write gitleaks sample Google API keys without a literal key ([#93](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/93)) ([76259f7](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/76259f79cb1770dd538ce4a7f804ab65e5cd98cb))

## [0.6.0](https://github.com/AdventureAsia365-Ecosys/AA-Booking/compare/v0.5.0...v0.6.0) (2026-10-24)


### Features

* accept inquiry.submitted v1.4 and link a signed-in traveller to one verified customer ([#90](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/90)) ([174a194](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/174a194c6fd80044f55c60b76084cd79b204397e))
* migration bk-023 (outbox supply stream, supply_version, entity_media, v_supply_* views) ([#84](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/84)) ([361e356](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/361e35660162496e9a159c7393879add2955088c))
* migration bk-024 (customer_identities, traveller_signin customer source) ([#87](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/87)) ([ae9d652](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/ae9d652562e199ce611903225da3e1952618504e))
* read-only session for the E2E staff user, plus a signed-in smoke note ([#89](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/89)) ([b1ba26b](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/b1ba26b12eae35724433daeebecbd5c19dd89836))
* supplier photo upload, CIS content read-back and published text on proposals (AA-835) ([#86](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/86)) ([f2eedbd](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/f2eedbdfd281f82ffec338325330a4edb239abab))


### Bug Fixes

* staff tables fit 1400 px with real Dev data shapes, no /cases layout shift ([#88](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/88)) ([0748333](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/074833394128e01e60ca5618609f4765c02b2b10))

## [0.5.0](https://github.com/AdventureAsia365-Ecosys/AA-Booking/compare/v0.4.0...v0.5.0) (2026-10-24)


### Features

* B2B event feed, on-request desk and departure incident flag (bk-022, booking.b2b.v1 1.1) ([#80](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/80)) ([4fb0eaf](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/4fb0eaf8ba75696e69be4b2fc7ecfd20a8112047))
* migration bk-022 (shared outbox, B2B event triggers, departure incidents, on-request desk) ([#77](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/77)) ([54e2594](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/54e2594e0cf68a37ac6389bc28ad101942200fd2))
* OpenAPI export and breaking-change check ([#82](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/82)) ([62c5e58](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/62c5e58b4e487719d258ea0d9d45ab833b7fde88))


### Bug Fixes

* trip plan facets count country-less tours, catalog read timeouts answer 503 ([#78](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/78)) ([4b8e8e2](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/4b8e8e20f75b22a4a80ca6f1e05a8d6954ab06b5))

## [0.4.0](https://github.com/AdventureAsia365-Ecosys/AA-Booking/compare/v0.3.0...v0.4.0) (2026-10-23)


### Features

* accept inquiry.submitted v1.3 (available and reason per leg and day) ([#76](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/76)) ([ab5641d](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/ab5641d1c90baf971441e2bf38140cf19bb27ce1))
* B2B commercial rules: VND quotes, cancel with fee and dry run, extend once, re-hold limit ([#71](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/71)) ([c314e00](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/c314e002bfe47e295a2e9de6830d9baed274736e))
* B2B departures, holds and confirm into a B2B trip case (booking.b2b.v1) ([#67](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/67)) ([d635c85](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/d635c85fb1d19a9f8c32505e9af0e7016e4f2d79))
* migration bk-020 (departures, B2B holds, B2B trip case) ([#65](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/65)) ([331e482](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/331e482df4f59d7d6919f6bb89f31b37d95ccd94))
* migration bk-021 (B2B cancellation policies, cancellations, hold extend, re-hold limit) ([#70](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/70)) ([30fc319](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/30fc319f9406b1ae9f752f72cd481ef397dfd6a8))
* refuse B2B sales of tours that left the catalog; staff flags, notifications and orphaned plans ([#75](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/75)) ([d5eea5a](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/d5eea5a95ba0879105d12eefb4c3669184000718))


### Bug Fixes

* Booking staff admin fits 1440 px and shows rate / superseded audit rows ([#68](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/68)) ([d7539bd](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/d7539bd48a2127c39d5eedff2eabc526f3bcce62))

## [0.3.0](https://github.com/AdventureAsia365-Ecosys/AA-Booking/compare/v0.2.0...v0.3.0) (2026-10-23)


### Features

* M2M auth and the B2B quote API (booking.b2b.v1) ([#64](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/64)) ([4b66d83](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/4b66d831851b9f9d4ab01b525758380fc60a86bd))
* migration bk-019 (B2B partners, quotes, trip plan components) ([#62](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/62)) ([8ba07e0](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/8ba07e052d00381829bfa5497615da5802b644de))

## [0.2.0](https://github.com/AdventureAsia365-Ecosys/AA-Booking/compare/v0.1.0...v0.2.0) (2026-10-22)


### Features

* AA-Booking staff admin frontend (wave 1) — CIS login BFF, dashboard, trip cases, proposal builder with pricing, customers, staff ([#5](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/5)) ([0933378](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/0933378e1a30f488a19e081292fd8efeebd238da))
* application_name booking-api on the DB pool ([#15](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/15)) ([5fcad05](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/5fcad05e32a2e4faa7c15c156068d4f712a0d8a0))
* bk-001 booking schema foundation and migration guard ([9e70fdc](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/9e70fdc49a80f7ce5cb8d73a4d16971f83d2183a))
* bk-007 policy approvals (maker-checker for pricing rules and FX rates) ([#8](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/8)) ([77eb750](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/77eb750ca828c503cb15f67d78ca0809dcdc1a2f))
* bk-008 suppliers and supplier contracts (wave 2a migration) ([#9](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/9)) ([34a4c27](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/34a4c27fe04a6b5fbec364c17e26d3c9d60dd4df))
* bk-009 trip_plans, commercial-only row per CIS catalog tour (migration only) ([#11](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/11)) ([ae7e2b1](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/ae7e2b1c785cbc16d5e1879c4401bf38abf45ceb))
* bk-010 hotels, room types, services and supplier net rates (wave 2b migration) ([#13](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/13)) ([51bb196](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/51bb196ebb33f676d6ee6e80ed671d9bfc28ca84))
* bk-011 catalog tour key columns on trip case legs (migration only) ([#17](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/17)) ([e74c60e](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/e74c60ed491394c94ffb09e1d45af3d6a92da5cf))
* bk-012 retention support on inbox_events and job (migration only) ([#22](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/22)) ([f7125d8](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/f7125d822acae201047ad1bc13b57d9f0fcd907c))
* bk-013 booking.job catches up with the job table template ([#24](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/24)) ([fe858a7](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/fe858a7898ed48993feb08dd7aa00219278e583a))
* bk-016 Cognito identity columns on booking.staff_users (migration only) ([#34](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/34)) ([afdf151](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/afdf1510f36a3d8fa98990e6ce5dffd4eef20f84))
* bk-018 PII and credential column tags (DM-6.1) ([#53](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/53)) ([2a4b57f](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/2a4b57f9550e43c04174ca2059fae404bac2f35e))
* Booking accepts Cognito WORKFORCE staff tokens next to the CIS bridge ([#35](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/35)) ([b3ad02f](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/b3ad02f1ec3308d24a6ca3738eef1dbc4b7f13bd))
* Booking deploy-gate endpoint authenticated by the deploy workflow's GitHub OIDC token ([#27](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/27)) ([bd3675a](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/bd3675ae6d57f53b780a94a88eada5808eac7948))
* Booking hotels, room types, services and supplier net rates (API + staff admin) ([#14](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/14)) ([2295f1b](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/2295f1b9d503fe40c0d7e4be8697ce07a65484c7))
* Booking retention kinds bk_retention_job and bk_retention_inbox_redact ([#23](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/23)) ([a35792c](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/a35792c138e9d170106c4cb45c643117d56c4afc))
* Booking staff sign-in with company email (Cognito booking-staff) ([#36](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/36)) ([eb51b78](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/eb51b78f9ad3bba5577c4e527eb3b9cafb9be2e2))
* booking.job backlog metrics sampler (AA/Jobs/Booking to CloudWatch) ([#26](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/26)) ([dc51c8b](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/dc51c8b842cbd3e4da1833c371ae63020412341e))
* CIS console link in the Booking staff topbar ([#30](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/30)) ([26da312](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/26da3126bf530515423feb14d655770c44a65f7d))
* FastAPI service with /booking/health and /booking/health/ready ([4879525](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/48795258cf99d881fc5cec5f801185efe5c82e48))
* FX rates and pricing rules admin with maker-checker (stacked on bk-007) ([#7](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/7)) ([8bb0271](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/8bb0271d23c031bb8f9fe090116a8ace606819a7))
* gate /booking/docs and openapi.json behind staff.admin outside test/local ([#52](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/52)) ([edd88ba](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/edd88ba48693fe2da7691bbc5a96f46885091a32))
* inquiry intake — bk-002 schema and booking.job worker (bk_inquiry_pull, bk_inquiry_process) ([#1](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/1)) ([81af463](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/81af463c3f0e395f79bf6f390e81a6b028f7b5e5))
* inquiry.submitted v1.2 intake fills the leg catalog tour key, leg key back-fill ([#18](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/18)) ([dda5521](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/dda5521d49a0c2b49b4da5b1cdd24da2917433c7))
* JSON logs with request ids and Sentry for the Booking API ([#57](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/57)) ([9d0d00d](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/9d0d00d12c8f9470e71a31cfb79603f471cdd667))
* ops overview contract views (funnel daily, case status) for CIS admin ([#37](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/37)) ([33a5d64](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/33a5d644c625a55781f696b46fe20c64e3740819))
* pricing engine (ADR 0005 first cut) — bk-004 rules/FX/item costs, quote-preview, price a version, bk-006 cost lock ([#4](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/4)) ([4063c3a](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/4063c3a5baee4ade765f0b9a90c75adc411911c9))
* release running jobs on shutdown, honour cancel_requested, job table template test ([#25](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/25)) ([a93d7d2](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/a93d7d2803228bd6db13e032b66512dbdd2bc213))
* release-please and the release version on /health ([#60](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/60)) ([446fc36](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/446fc36014f13e8c0efcdc01987dcdc0b6a9c0a0))
* sellable trip plans over catalog.v_tour (API + staff admin /catalog/trip-plans) ([#12](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/12)) ([60e8016](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/60e801690b830311a29e649578734999a79916b4))
* staff admin wave 1 close-out — audit log page, per-entry nav flag, card fee row, dashboard hint fix ([#6](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/6)) ([1a68edf](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/1a68edf27348916eaa1a4633a3c79d23f135c2be))
* staff JWT auth, trip case workflow and proposal versions (bk-003) ([#2](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/2)) ([57dafdc](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/57dafdc668effa6f005abcc9953806c13bace89c))
* staff new-inquiry e-mail through SES (bk_notify_email job) ([#16](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/16)) ([9d2ff9b](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/9d2ff9b40e3cc7e3f52dd3b18a9fe733bd6d35f5))
* staff sign-in on the fixed Preview host through a read-only preview client ([#54](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/54)) ([dcb9936](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/dcb9936a321e56398733fe55c9d1edbf89286f5e))
* suppliers and supplier contracts API + staff admin pages (wave 2a) ([#10](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/10)) ([912e560](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/912e560883e577f0f0d0d0613751ae7798a3b5b6))
* wave 1 staff admin backend (bk-005) — dashboard, notifications, customers, settings, approve/reopen ([#3](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/3)) ([a932cc5](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/a932cc572dfec568f3c2907144f8e9ea2409732d))
* X1 bk-014 transfers booking ownership to bk_owner ([#32](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/32)) ([9f29c34](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/9f29c34c5aeaed5b598680438d0d4c03a9fdbabf))
* X1 S9 set-role probe migration ([#33](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/33)) ([6182045](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/6182045c8b085b0fe1ca6dda4f4d709a2942732a))


### Bug Fixes

* Booking defaults use the custom domains (booking/api-booking.lumiguides.it.com) ([#41](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/41)) ([e6a91c9](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/e6a91c96f7dfd28d7b0ee5babedc3b5bd5ca95ee))
* kit modal body scrolls and keeps its footer above the phone bottom nav ([#21](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/21)) ([17099c3](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/17099c34573ddc3a5172792745af25e52afe6638))
* pricing rules and proposals tables fit 1440 px without sideways scroll ([#20](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/20)) ([cd4b962](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/cd4b962aa4007d8c8fae1bc96f54a17221956fdd))
* send the Sentry SDK's own contexts unscrubbed ([#59](https://github.com/AdventureAsia365-Ecosys/AA-Booking/issues/59)) ([0852435](https://github.com/AdventureAsia365-Ecosys/AA-Booking/commit/0852435e2d27c4c57cdb7574424f89864dd0fc70))
