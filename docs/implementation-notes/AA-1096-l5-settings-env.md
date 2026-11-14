# AA-1096 lane l5-settings-env (S281) — no env var for a policy parameter (Booking)

Last Done-when of AA-1096: "No env var remains for a migrated setting after one release". Booking's admin-editable
values are the parameters of `booking.pricing_policy` (Pricing › Policy). They came from code literals, never from env
vars: `booking.pricing_policy` and `booking.settings` have no env-override column (db-schema-reference), the reader
has no env step, and AA-CIS-Infra sets none of them on the task (lane L6 checked bk-056). So Booking has nothing to
retire: no migration, no code change.

## Decisions

- **Settings Lint rule 7** (`backend/tests/unit/test_aa1096_settings_lint.py`, the required "Settings Lint" job): no
  module of `app/` reads an env var (any type, not only a number as rule 2 already refuses) named after a parameter
  (`<KEY>` or `BOOKING_<KEY>`, upper case). The drift the other apps just removed cannot start here. Self-tested on a
  planted file.

## Changed

- `backend/tests/unit/test_aa1096_settings_lint.py` only.

## Tradeoffs

- The rule matches by name (the parameter key in upper case). An env var with an unrelated name that feeds a policy
  value would pass it; rule 2 still refuses it when it is numeric, the common case.

## Should know

- `git grep` proof on main (15/11/2026): the only env reads in `app/` are deployment wiring (`DATABASE_URL`,
  `DB_SSL`, `ENVIRONMENT`, `ALERTS_TOPIC_ARN`, the retention override gate, `app/settings.py` identity and service
  endpoints); none is a policy parameter.
