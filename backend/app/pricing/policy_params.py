"""The pricing policy parameters: keys, types, units and allowed ranges (root ADR 0005b decision 15; AA-1091, bk-050).

The tech lead's rule (S278): every business policy value is adjustable on a Booking staff page (Pricing > Policy),
never a constant in code. So this module holds only what a value **is** (its type, unit, the question it answers,
who approves it and the range the code accepts) and never what it is set to: the values live in
``booking.pricing_policy`` (seeded by bk-050 with the answers the tech lead approved on 2026-11-12) and are read
through :mod:`app.pricing.policy_values`. A range here is the code's only bound (ADR 0005b: "Booking validates a new
value against the allowed range and refuses anything outside it").

Value shapes, as stored in ``pricing_policy.value`` (jsonb) and as the staff API takes and answers them:

- ``integer``: a JSON number (``14``);
- ``decimal``: a JSON string of a decimal number (``"5"`` = 5 %), never a float;
- ``boolean``: ``true`` / ``false``;
- ``month_day``: ``"MM-DD"`` (``"11-01"``); ``month_days``: a sorted list of those;
- ``codes``: a sorted list of codes from ``choices`` (``["fx", "tax"]``);
- ``currency_map``: ``{"<ISO country>" | "*": "<ISO currency>"}`` with a ``"*"`` (every other country) entry;
- ``integers``: a sorted list of distinct whole numbers, each inside the range (``[1, 7, 14]``);
- ``amount_map``: ``{"<ISO currency>": "<decimal>"}`` with exactly the currencies of ``choices``, each amount inside
  the range (``{"USD": "5", ...}``).

S279 (thresholds program, lane L3, bk-056): the threshold and policy values Booking kept in ``booking.settings`` or in
code (hold length and limits, the on-request SLA, traveller ages, the departure decision day, the operations
reminders, agency invoice reminder steps, rounding, retention windows) are parameters here too, so the one page
Pricing > Policy edits every Booking policy value. Each parameter belongs to a ``section`` (the page groups by it). A
parameter whose feature is not built yet reads ``"stored"`` in ``used_by`` and says why in ``pending``; the page
badges it "not enforced yet" (and the CI guard checks every other one is read by its module).

Approver groups (ADR 0005b d6, Q5): ``pricing`` = the product or operations lead (``pricing_policy.approve``),
``finance`` = finance approves FX, tax, surcharge and payment parameters (``pricing_policy.approve_finance``),
``admin`` = the administrator approves data retention windows (``pricing_policy.approve_admin``; S279: a retention
window is a DM-7.5 decision).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

INTEGER = "integer"
DECIMAL = "decimal"
BOOLEAN = "boolean"
MONTH_DAY = "month_day"
MONTH_DAYS = "month_days"
CODES = "codes"
CURRENCY_MAP = "currency_map"
INTEGERS = "integers"
AMOUNT_MAP = "amount_map"
TYPES = (INTEGER, DECIMAL, BOOLEAN, MONTH_DAY, MONTH_DAYS, CODES, CURRENCY_MAP, INTEGERS, AMOUNT_MAP)

PRICING = "pricing"
FINANCE = "finance"
ADMIN = "admin"
APPROVE_PERMISSION = {
    PRICING: "pricing_policy.approve",
    FINANCE: "pricing_policy.approve_finance",
    ADMIN: "pricing_policy.approve_admin",
}

# The page sections, in page order (the staff page groups the parameters by them; labels live in the page's
# messages, EN and VI).
SECTIONS = (
    "quotes",
    "prices",
    "surcharges",
    "payments",
    "holds",
    "desk",
    "travellers",
    "departures",
    "operations",
    "agency_billing",
    "rounding",
    "retention",
)
# The selling currencies a rounding increment is required for (app.pricing.engine.SELLING_CURRENCIES; the engine
# imports nothing from here, the unit test keeps the two equal).
ROUNDING_CURRENCIES = ("AUD", "EUR", "GBP", "SGD", "USD")

MONTH_DAY_RE = re.compile(r"^(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])$")
COUNTRY_RE = re.compile(r"^(\*|[A-Z]{2})$")
CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
MAX_LIST = 24


class PolicyValueError(ValueError):
    """A value outside its type or allowed range; the message says what is allowed."""


@dataclass(frozen=True)
class Param:
    key: str
    type: str
    unit: str | None
    question: str  # the ADR 0005b d15 question (Q1 ... Q12) or decision it answers
    group: str  # "pricing" or "finance": who approves a change
    label: str  # short English label for the staff page
    help: str  # one sentence: what the value does
    used_by: str  # where the code reads it today, or "stored" (read by a later slice, shown as such)
    section: str = "quotes"  # one of SECTIONS: where the page lists it
    pending: str = ""  # used_by "stored" only: what is missing before the code reads it (shown on the page)
    minimum: Decimal | None = None
    maximum: Decimal | None = None
    choices: tuple[str, ...] = ()
    # A bound that depends on another parameter's current value: returns an error text or None.
    cross_check: Callable[[Any, Mapping[str, Any]], str | None] | None = field(default=None, compare=False)


def _festive_not_longer(value: Any, current: Mapping[str, Any]) -> str | None:
    days = current.get("quote_validity_days")
    if isinstance(days, int) and value > days:
        return f"must not be longer than quote_validity_days ({days})"
    return None


def _validity_covers_festive(value: Any, current: Mapping[str, Any]) -> str | None:
    festive = current.get("quote_validity_festive_days")
    if isinstance(festive, int) and value < festive:
        return f"must not be shorter than quote_validity_festive_days ({festive})"
    return None


def _publish_before_start(value: Any, current: Mapping[str, Any]) -> str | None:
    start = current.get("tariff_year_start")
    if isinstance(start, str) and value == start:
        return "must be another day than tariff_year_start"
    return None


def _infant_below_child(value: Any, current: Mapping[str, Any]) -> str | None:
    child = current.get("b2b_child_age_max")
    if isinstance(child, int) and value >= child:
        return f"must be below b2b_child_age_max ({child})"
    return None


def _child_above_infant(value: Any, current: Mapping[str, Any]) -> str | None:
    infant = current.get("b2b_infant_age_max")
    if isinstance(infant, int) and value <= infant:
        return f"must be above b2b_infant_age_max ({infant})"
    return None


def _failed_not_shorter(value: Any, current: Mapping[str, Any]) -> str | None:
    succeeded = current.get("retention_job_succeeded_days")
    if isinstance(succeeded, int) and value < succeeded:
        return f"must not be shorter than retention_job_succeeded_days ({succeeded})"
    return None


def _succeeded_not_longer(value: Any, current: Mapping[str, Any]) -> str | None:
    failed = current.get("retention_job_failed_days")
    if isinstance(failed, int) and value > failed:
        return f"must not be longer than retention_job_failed_days ({failed})"
    return None


SURCHARGES_PENDING = "Surcharges after confirmation are not built yet."
TERMS_PENDING = "Agency invoices follow the deposit and balance terms on the Agency billing page today."


def _d(text: str) -> Decimal:
    return Decimal(text)


PARAMS: dict[str, Param] = {
    p.key: p
    for p in (
        Param(
            "tariff_year_start",
            MONTH_DAY,
            "MM-DD",
            "Q1",
            PRICING,
            "Tariff year starts",
            "First day of the agency tariff year; the year runs to the day before it.",
            "app.catalog.price_versions",
            section="quotes",
        ),
        Param(
            "tariff_next_publish_by",
            MONTH_DAY,
            "MM-DD",
            "Q1",
            PRICING,
            "Next tariff published by",
            "The day the next tariff year should be published; a reminder date, it blocks nothing.",
            "stored",
            cross_check=_publish_before_start,
            section="quotes",
            pending="No reminder of the next tariff publication is built yet.",
        ),
        Param(
            "quote_validity_days",
            INTEGER,
            "days",
            "Q2",
            PRICING,
            "Quote validity",
            "How long a B2B quote is valid.",
            "app.b2b.quotes",
            minimum=_d("1"),
            maximum=_d("30"),
            cross_check=_validity_covers_festive,
            section="quotes",
        ),
        Param(
            "quote_validity_festive_days",
            INTEGER,
            "days",
            "Q2",
            PRICING,
            "Quote validity, festive periods",
            "Validity of a quote that touches a festive period (Tet, Christmas, New Year) of the season calendar.",
            "app.b2b.quotes",
            minimum=_d("1"),
            maximum=_d("30"),
            cross_check=_festive_not_longer,
            section="quotes",
        ),
        Param(
            "surcharge_causes",
            CODES,
            None,
            "Q3",
            FINANCE,
            "Surcharge causes after confirmation",
            "The only causes that may add a surcharge line to a confirmed booking; empty = never.",
            "stored",
            choices=("fuel", "fx", "tax"),
            section="surcharges",
            pending=SURCHARGES_PENDING,
        ),
        Param(
            "surcharge_fx_threshold_pct",
            DECIMAL,
            "%",
            "Q3",
            FINANCE,
            "FX move that allows a surcharge",
            "An FX move counts only above this percentage against the rate locked on the booking.",
            "stored",
            minimum=_d("0.5"),
            maximum=_d("50"),
            section="surcharges",
            pending=SURCHARGES_PENDING,
        ),
        Param(
            "surcharge_notice_days",
            INTEGER,
            "days",
            "Q3",
            FINANCE,
            "Surcharge notice",
            "Days before departure the agency must be told of a surcharge (20 is the legal minimum).",
            "stored",
            minimum=_d("20"),
            maximum=_d("90"),
            section="surcharges",
            pending=SURCHARGES_PENDING,
        ),
        Param(
            "surcharge_free_cancel_pct",
            DECIMAL,
            "%",
            "Q3",
            FINANCE,
            "Surcharge that allows a free cancellation",
            "Above this share of the booking price the agency may cancel free of charge (8 is the legal maximum).",
            "stored",
            minimum=_d("1"),
            maximum=_d("8"),
            section="surcharges",
            pending=SURCHARGES_PENDING,
        ),
        Param(
            "recompute_auto_publish_pct",
            DECIMAL,
            "%",
            "Q4",
            PRICING,
            "Recompute auto-publish threshold",
            "A recomputed price version publishes by itself when every row moves within this percentage; 0 = never.",
            "stored",
            minimum=_d("0"),
            maximum=_d("10"),
            section="prices",
            pending="A recompute stores the computed prices directly; there is no approval step to skip yet.",
        ),
        Param(
            "diff_highlight_pct",
            DECIMAL,
            "%",
            "d9",
            PRICING,
            "Highlight price changes above",
            "Rows of a price change that move more than this are highlighted for the approver.",
            "app.catalog.price_versions",
            minimum=_d("1"),
            maximum=_d("50"),
            section="prices",
        ),
        Param(
            "provisional_uplift_pct",
            DECIMAL,
            "%",
            "Q12",
            PRICING,
            "Provisional price uplift",
            "A provisional price is last year's same-season price plus this percentage.",
            "stored",
            minimum=_d("0"),
            maximum=_d("25"),
            section="prices",
            pending="Provisional prices from last year's price are not built yet (the workbench shows the CIS price).",
        ),
        Param(
            "provisional_cis_ref_quotable",
            BOOLEAN,
            None,
            "Q12",
            PRICING,
            "Quote provisional CIS prices",
            "Whether a provisional price from the CIS reference may be quoted to agencies (else display only).",
            "stored",
            section="prices",
            pending="The CIS reference price is display only; quoting it is not built.",
        ),
        Param(
            "group_on_request_min_pax",
            INTEGER,
            "travellers",
            "Q9",
            PRICING,
            "Group quoted on request from",
            "A tour line with this many travellers or more is quoted on request.",
            "app.b2b.quotes",
            minimum=_d("2"),
            maximum=_d("60"),
            section="quotes",
        ),
        Param(
            "foc_one_per_pax",
            INTEGER,
            "travellers",
            "Q9",
            PRICING,
            "Free place per",
            "Default free places (FOC) when a supplier contract states none: one per this many travellers.",
            "stored",
            minimum=_d("5"),
            maximum=_d("60"),
            section="prices",
            pending="Free places (FOC) are not priced yet.",
        ),
        Param(
            "fit_full_payment_days",
            INTEGER,
            "days",
            "Q8",
            FINANCE,
            "Individual booking paid in full",
            "Days before departure an individual (FIT) booking is paid in full.",
            "stored",
            minimum=_d("0"),
            maximum=_d("180"),
            section="payments",
            pending=TERMS_PENDING,
        ),
        Param(
            "group_deposit_pct",
            DECIMAL,
            "%",
            "Q8",
            FINANCE,
            "Group deposit",
            "Deposit a group pays, as a percentage of the booking price.",
            "stored",
            minimum=_d("0"),
            maximum=_d("100"),
            section="payments",
            pending=TERMS_PENDING,
        ),
        Param(
            "group_deposit_days",
            INTEGER,
            "days",
            "Q8",
            FINANCE,
            "Group deposit due",
            "Days before departure the group deposit is due.",
            "stored",
            minimum=_d("0"),
            maximum=_d("365"),
            section="payments",
            pending=TERMS_PENDING,
        ),
        Param(
            "festive_non_refundable_days",
            INTEGER,
            "days",
            "Q8",
            FINANCE,
            "Festive bookings non-refundable within",
            "A booking in a festive period is non-refundable within this many days of departure.",
            "stored",
            minimum=_d("0"),
            maximum=_d("365"),
            section="payments",
            pending="The cancellation fee follows the cancellation fee schedule only today.",
        ),
        Param(
            "gala_dinner_days",
            MONTH_DAYS,
            "MM-DD",
            "Q7",
            PRICING,
            "Compulsory gala dinner days",
            "Days a compulsory gala dinner is charged at cost plus the standard margin.",
            "stored",
            section="prices",
            pending="Gala dinner lines are not added to quotes yet.",
        ),
        Param(
            "agency_currency_by_country",
            CURRENCY_MAP,
            None,
            "Q11",
            FINANCE,
            "Agency currency by country",
            "The quote currency of an agency by its country; * is every other country.",
            "stored",
            section="payments",
            pending="New agencies are billed in USD; the CIS tenant account carries no country yet.",
        ),
        # ── S279 L3 (bk-056): the values that were booking.settings rows or code constants ──
        Param(
            "b2b_hold_hours",
            INTEGER,
            "hours",
            "S279",
            PRICING,
            "Hold length",
            "How long a B2B hold keeps its seats; capped by the departure start.",
            "app.b2b.holds",
            section="holds",
            minimum=_d("1"),
            maximum=_d("720"),
        ),
        Param(
            "b2b_hold_extend_hours",
            INTEGER,
            "hours",
            "S279",
            PRICING,
            "Hold extension",
            "Hours a hold gains when the agency extends it; capped by the departure start.",
            "app.b2b.holds",
            section="holds",
            minimum=_d("1"),
            maximum=_d("720"),
        ),
        Param(
            "b2b_hold_max_extensions",
            INTEGER,
            "extensions",
            "S279",
            PRICING,
            "Extensions per hold",
            "How many times one hold may be extended; 0 = never (the database allows at most one).",
            "app.b2b.holds",
            section="holds",
            minimum=_d("0"),
            maximum=_d("1"),
        ),
        Param(
            "b2b_rehold_limit_per_day",
            INTEGER,
            "holds",
            "S279",
            PRICING,
            "Re-holds per day",
            "Re-holds an agency may place per UTC day, per proposal and per departure, before it is refused.",
            "app.b2b.holds",
            section="holds",
            minimum=_d("0"),
            maximum=_d("100"),
        ),
        Param(
            "b2b_on_request_answer_business_days",
            INTEGER,
            "business days",
            "S279",
            PRICING,
            "On-request answer deadline",
            "Business days (ops time zone) the desk has to accept or decline an on-request line of a booking.",
            "app.b2b.desk",
            section="desk",
            minimum=_d("1"),
            maximum=_d("10"),
        ),
        Param(
            "b2b_on_request_due_soon_hours",
            INTEGER,
            "hours",
            "S279",
            PRICING,
            "On-request due soon",
            "An on-request line shows as due soon this many hours before its answer deadline.",
            "app.b2b.desk",
            section="desk",
            minimum=_d("1"),
            maximum=_d("24"),
        ),
        Param(
            "b2b_child_age_max",
            INTEGER,
            "years",
            "S279",
            PRICING,
            "Child price up to age",
            "Oldest age at the child price when a trip plan sets none.",
            "app.b2b.reads",
            section="travellers",
            minimum=_d("2"),
            maximum=_d("17"),
            cross_check=_child_above_infant,
        ),
        Param(
            "b2b_infant_age_max",
            INTEGER,
            "years",
            "S279",
            PRICING,
            "Infant price up to age",
            "Oldest age at the infant price when a trip plan sets none.",
            "app.b2b.reads",
            section="travellers",
            minimum=_d("0"),
            maximum=_d("4"),
            cross_check=_infant_below_child,
        ),
        Param(
            "departure_decision_days_default",
            INTEGER,
            "days",
            "S279",
            PRICING,
            "Departure decision day",
            "A new departure decides go or cancel this many days before it starts, unless staff set another day.",
            "app.b2b.departures",
            section="departures",
            minimum=_d("0"),
            maximum=_d("365"),
        ),
        Param(
            "pre_departure_days",
            INTEGER,
            "days",
            "S279",
            PRICING,
            "Pre-departure starts",
            "Days before the start date a fully booked trip case moves to pre-departure.",
            "app.ops.lifecycle",
            section="operations",
            minimum=_d("0"),
            maximum=_d("120"),
        ),
        Param(
            "supplier_answer_days",
            INTEGER,
            "days",
            "S279",
            PRICING,
            "Supplier answer reminder",
            "Days a requested supplier booking may wait for the supplier's answer before staff are reminded.",
            "app.ops.lifecycle",
            section="operations",
            minimum=_d("1"),
            maximum=_d("60"),
        ),
        Param(
            "trip_check_reminder_days",
            INTEGER,
            "days",
            "S279",
            PRICING,
            "Trip check reminder",
            "Days before its due date an open trip check reminds its owner.",
            "app.ops.lifecycle",
            section="operations",
            minimum=_d("0"),
            maximum=_d("60"),
        ),
        Param(
            "supplier_bill_due_soon_days",
            INTEGER,
            "days",
            "S279",
            FINANCE,
            "Supplier bill due soon",
            "Days before its due date an unpaid supplier bill is announced to staff.",
            "app.ops.lifecycle",
            section="operations",
            minimum=_d("0"),
            maximum=_d("60"),
        ),
        Param(
            "agency_reminder_steps_days",
            INTEGERS,
            "days",
            "S279",
            FINANCE,
            "Overdue invoice reminders",
            "Days after the due date an unpaid agency invoice is reminded by e-mail, once per step.",
            "app.agency_billing.reminders",
            section="agency_billing",
            minimum=_d("1"),
            maximum=_d("365"),
        ),
        Param(
            "rounding_increment_by_currency",
            AMOUNT_MAP,
            None,
            "S279",
            PRICING,
            "Price rounding per currency",
            "Customer-facing totals are rounded up to this amount of each selling currency.",
            "app.pricing.service",
            section="rounding",
            minimum=_d("0.01"),
            maximum=_d("1000"),
            choices=ROUNDING_CURRENCIES,
        ),
        Param(
            "retention_job_succeeded_days",
            INTEGER,
            "days",
            "S279",
            ADMIN,
            "Finished jobs kept",
            "Succeeded background jobs are archived and deleted this many days after they finish.",
            "app.retention.policy",
            section="retention",
            minimum=_d("7"),
            maximum=_d("365"),
            cross_check=_succeeded_not_longer,
        ),
        Param(
            "retention_job_failed_days",
            INTEGER,
            "days",
            "S279",
            ADMIN,
            "Failed jobs kept",
            "Failed and cancelled background jobs are archived and deleted this many days after they finish.",
            "app.retention.policy",
            section="retention",
            minimum=_d("30"),
            maximum=_d("730"),
            cross_check=_failed_not_shorter,
        ),
        Param(
            "retention_inbox_redact_days",
            INTEGER,
            "days",
            "S279",
            ADMIN,
            "Inquiry personal data kept",
            "Personal data in received inquiry payloads is redacted this many days after receipt.",
            "app.retention.policy",
            section="retention",
            minimum=_d("30"),
            maximum=_d("365"),
        ),
        Param(
            "emergency_contact_keep_days",
            INTEGER,
            "days",
            "S279",
            ADMIN,
            "Emergency contacts kept",
            "Days after the trip ends that the emergency contact and trip check notes are kept, then cleared.",
            "app.ops.lifecycle",
            section="retention",
            minimum=_d("0"),
            maximum=_d("3650"),
        ),
    )
}


def _number(value: Any, key: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise PolicyValueError(f"{key}: a number is required")
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise PolicyValueError(f"{key}: a number is required") from None
    if not number.is_finite():
        raise PolicyValueError(f"{key}: a number is required")
    return number


def _bounds(param: Param, number: Decimal) -> None:
    if param.minimum is not None and number < param.minimum:
        raise PolicyValueError(f"{param.key}: at least {param.minimum}")
    if param.maximum is not None and number > param.maximum:
        raise PolicyValueError(f"{param.key}: at most {param.maximum}")


def _month_day(text: Any, key: str) -> str:
    if not isinstance(text, str) or not MONTH_DAY_RE.match(text):
        raise PolicyValueError(f"{key}: a month-day MM-DD is required")
    month, day = (int(x) for x in text.split("-"))
    try:
        date(2028, month, day)  # a leap year, so 02-29 is a valid month-day
    except ValueError:
        raise PolicyValueError(f"{key}: {text} is not a day of the year") from None
    return text


def normalise(key: str, value: Any, current: Mapping[str, Any] | None = None) -> Any:
    """The stored shape of ``value`` for parameter ``key`` (see the module docstring), or :class:`PolicyValueError`.

    ``current`` (the values in force, by key) feeds the cross-parameter bounds (festive validity <= validity)."""
    param = PARAMS.get(key)
    if param is None:
        raise PolicyValueError(f"unknown policy parameter {key}")
    out: Any
    if param.type == INTEGER:
        number = _number(value, key)
        if number != number.to_integral_value():
            raise PolicyValueError(f"{key}: a whole number is required")
        _bounds(param, number)
        out = int(number)
    elif param.type == DECIMAL:
        number = _number(value, key)
        if number.as_tuple().exponent < -2:  # type: ignore[operator]
            raise PolicyValueError(f"{key}: at most 2 decimals")
        _bounds(param, number)
        out = format(number.normalize(), "f")
    elif param.type == BOOLEAN:
        if not isinstance(value, bool):
            raise PolicyValueError(f"{key}: true or false is required")
        out = value
    elif param.type == MONTH_DAY:
        out = _month_day(value, key)
    elif param.type == MONTH_DAYS:
        if not isinstance(value, list) or len(value) > MAX_LIST:
            raise PolicyValueError(f"{key}: a list of at most {MAX_LIST} month-days is required")
        out = sorted({_month_day(v, key) for v in value})
    elif param.type == CODES:
        if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
            raise PolicyValueError(f"{key}: a list of codes is required")
        unknown = sorted(set(value) - set(param.choices))
        if unknown:
            raise PolicyValueError(f"{key}: unknown code(s) {', '.join(unknown)} (allowed: {', '.join(param.choices)})")
        out = sorted(set(value))
    elif param.type == CURRENCY_MAP:
        if not isinstance(value, dict) or not value or len(value) > MAX_LIST:
            raise PolicyValueError(f"{key}: a map of country to currency is required")
        out = {}
        for country, currency in value.items():
            if not isinstance(country, str) or not COUNTRY_RE.match(country):
                raise PolicyValueError(f"{key}: {country!r} is not an ISO country code or *")
            if not isinstance(currency, str) or not CURRENCY_RE.match(currency):
                raise PolicyValueError(f"{key}: {currency!r} is not an ISO currency code")
            out[country] = currency
        if "*" not in out:
            raise PolicyValueError(f"{key}: an entry for * (every other country) is required")
        out = dict(sorted(out.items()))
    elif param.type == INTEGERS:
        if not isinstance(value, list) or not value or len(value) > MAX_LIST:
            raise PolicyValueError(f"{key}: a list of 1 to {MAX_LIST} whole numbers is required")
        numbers = set()
        for item in value:
            number = _number(item, key)
            if number != number.to_integral_value():
                raise PolicyValueError(f"{key}: whole numbers are required")
            _bounds(param, number)
            numbers.add(int(number))
        out = sorted(numbers)
    elif param.type == AMOUNT_MAP:
        if not isinstance(value, dict):
            raise PolicyValueError(f"{key}: a map of currency to amount is required")
        if set(value) != set(param.choices):
            raise PolicyValueError(f"{key}: exactly one amount per currency is required ({', '.join(param.choices)})")
        out = {}
        for currency in sorted(value):
            number = _number(value[currency], key)
            if number.as_tuple().exponent < -2:  # type: ignore[operator]
                raise PolicyValueError(f"{key}: at most 2 decimals")
            _bounds(param, number)
            out[currency] = format(number.normalize(), "f")
    else:  # pragma: no cover - PARAMS only uses the types above (unit test)
        raise PolicyValueError(f"{key}: unknown type {param.type}")
    if param.cross_check is not None and current:
        problem = param.cross_check(out, current)
        if problem:
            raise PolicyValueError(f"{key}: {problem}")
    return out


def describe(param: Param) -> dict[str, Any]:
    """The parameter's definition for the staff page (no value)."""
    return {
        "key": param.key,
        "type": param.type,
        "unit": param.unit,
        "question": param.question,
        "approver": param.group,
        "label": param.label,
        "help": param.help,
        "in_use": param.used_by != "stored",
        "section": param.section,
        # S279: a parameter no code reads yet is badged "not enforced yet" on the page, with what is missing.
        "not_enforced_reason": param.pending or None,
        "minimum": None if param.minimum is None else format(param.minimum, "f"),
        "maximum": None if param.maximum is None else format(param.maximum, "f"),
        "choices": list(param.choices),
    }
