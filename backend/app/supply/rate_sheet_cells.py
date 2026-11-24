"""Cell values and small-sheet reading shared by the rate-sheet importer (:mod:`app.supply.rate_sheet`), its Inventory
sheet (:mod:`app.supply.rate_sheet_inventory`) and the activity catalogue import (:mod:`app.supply.activity_import`):
every problem is a :class:`Problem` (sheet, row, column, code, message), never an exception, so one upload lists them
all (split out of :mod:`app.supply.rate_sheet`: file size limit; AA-1153 part 2)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.errors import Invalid

PAX_TYPES = ("adult", "child", "infant")
AGE_MAX = 17  # technical-constant: the bk-060 CHECK bound (a child or infant is under 18)


@dataclass
class Problem:
    sheet: str
    row: int | None
    column: str | None
    code: str
    message: str

    def out(self) -> dict[str, Any]:
        return {"sheet": self.sheet, "row": self.row, "column": self.column, "code": self.code, "message": self.message}


@dataclass
class Budget:
    """The rows read so far against the workbook's cap (AA-1153 part 2: the pricing policy ``rate_sheet_max_rows`` in
    force on the import day; None = no cap, a parse outside an import)."""

    cap: int | None = None
    used: int = 0

    def take(self, n: int = 1) -> None:
        """Count ``n`` rows (rate rows, seasons, child prices, inventory rows, expanded rates); 422
        ``rate_sheet_too_many_rows`` past the cap."""
        self.used += n
        if self.cap is not None and self.used > self.cap:
            raise Invalid("rate_sheet_too_many_rows", f"a rate sheet has at most {self.cap} rows")


def _key(text: Any) -> str:
    return " ".join(str(text or "").split()).lower()


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text or None


def _date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt).date()  # noqa: DTZ007 — a calendar date, no time zone
        except ValueError:
            continue
    raise ValueError("use a date (YYYY-MM-DD)")


def _amount(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("a number is expected")
    try:
        number = Decimal(str(value).replace(",", "").strip())
    except InvalidOperation:
        raise ValueError("a number is expected") from None
    if not number.is_finite() or number < 0:
        raise ValueError("a zero or positive number is expected")
    if number != number.quantize(Decimal("0.01")):
        raise ValueError("at most 2 decimals")
    return number


def _pct(value: Any) -> Decimal | None:
    number = _amount(value)
    if number is not None and number >= 100:
        raise ValueError("a commission below 100 % is expected")
    return number


def _age(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation:
        raise ValueError("a whole number is expected") from None
    if number != number.to_integral_value() or not 0 <= number <= AGE_MAX:
        raise ValueError(f"a whole number from 0 to {AGE_MAX} is expected")
    return int(number)


def _pax_type(value: Any) -> str | None:
    text = (_text(value) or "").lower()
    if not text:
        return None
    if text not in PAX_TYPES:
        raise ValueError(f"one of {', '.join(PAX_TYPES)} is expected")
    return text


def _int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation:
        raise ValueError("a whole number is expected") from None
    if number != number.to_integral_value() or not 1 <= number <= 999:
        raise ValueError("a whole number from 1 to 999 is expected")
    return int(number)


def _table(
    ws: Any, sheet: str, columns: tuple[str, ...], problems: list[Problem], budget: Budget
) -> list[tuple[int, dict[str, Any]]]:
    """The rows of a small sheet as ``(row number, {column: raw value})``; every column is required. Each row counts
    against the workbook's cap (:class:`Budget`)."""
    rows = ws.iter_rows(values_only=True)
    header = next(rows, None)
    if header is None:
        return []
    index = {_key(h): i for i, h in enumerate(header) if h is not None}
    missing = [c for c in columns if _key(c) not in index]
    if missing:
        problems.append(
            Problem(sheet, 1, ", ".join(missing), "missing_column", f"missing column(s): {', '.join(missing)}")
        )
        return []
    out = []
    for n, row in enumerate(rows, start=2):
        if row is None or all(v is None or str(v).strip() == "" for v in row):
            continue
        budget.take()
        out.append((n, {c: row[index[_key(c)]] if index[_key(c)] < len(row) else None for c in columns}))
    return out
