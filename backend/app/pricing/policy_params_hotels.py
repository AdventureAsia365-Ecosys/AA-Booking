"""The hotel contracting parameters of the pricing policy registry (AA-1153 part 1, bk-069; S285 advisory r2 hotels T8,
T10, T11; tech lead S279: every threshold is an admin-edited setting, never a code literal).

Section "hotels" of Pricing > Policy, approved by the pricing group. :mod:`app.pricing.policy_params` builds a
:class:`~app.pricing.policy_params.Param` from each spec here (plain data, so this module imports nothing from it).

- the four duplicate-hotel thresholds and the facts re-confirmation interval: read by :mod:`app.supply.hotel_master`
  (hotel create / edit and the hotel list);
- the price jump a rate-sheet import flags against the supplier's previous contract: read by
  :mod:`app.supply.rate_sheet` (the import and the review before publishing);
- the default board of a hotel night (T9): the board a room night is priced on when neither the quote line nor the
  tour night names one, read by :mod:`app.supply.rates` (every rate lookup: B2B quotes and tariffs, version item
  defaults, the cost sheet, the staff rate lookup);
- AA-1153 part 2 (bk-072, B8 / T10): the release days an allotment term gets when it names none, read by
  :mod:`app.supply.restrictions` (the term form) and :mod:`app.supply.rate_sheet_inventory` (the Inventory sheet);
  the most rows of one rate-sheet workbook, read by :mod:`app.supply.rate_sheet` (import) and
  :mod:`app.supply.rate_sheet_export` (export: a bigger contract is refused, so an exported sheet always imports back).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

MATCH_BLOCK_PCT = "hotel_match_block_pct"
MATCH_BLOCK_M = "hotel_match_block_m"
MATCH_WARN_PCT = "hotel_match_warn_pct"
MATCH_WARN_M = "hotel_match_warn_m"
FACTS_RECONFIRM_DAYS = "hotel_facts_reconfirm_days"
JUMP_WARN_PCT = "rate_sheet_jump_warn_pct"
DEFAULT_BOARD = "hotel_default_board"
RELEASE_DAYS_DEFAULT = "allotment_release_days_default"
SHEET_MAX_ROWS = "rate_sheet_max_rows"
BOARDS = ("RO", "BB", "HB", "FB", "AI")  # = app.supply.rates.MEAL_PLANS (a unit test keeps them equal)
MATCH_KEYS = (MATCH_BLOCK_PCT, MATCH_BLOCK_M, MATCH_WARN_PCT, MATCH_WARN_M)

_MATCH = "app.supply.hotel_master"
SPECS: tuple[dict[str, Any], ...] = (
    {
        "key": MATCH_BLOCK_PCT,
        "type": "integer",
        "unit": "%",
        "question": "S285",
        "group": "pricing",
        "label": "Duplicate hotel: name similarity that blocks (%)",
        "help": "A new hotel whose cleaned name is at least this similar to a hotel close by is refused as the same "
        "hotel.",
        "used_by": _MATCH,
        "section": "hotels",
        "minimum": Decimal("50"),
        "maximum": Decimal("100"),
    },
    {
        "key": MATCH_BLOCK_M,
        "type": "integer",
        "unit": "metres",
        "question": "S285",
        "group": "pricing",
        "label": "Duplicate hotel: distance that blocks (metres)",
        "help": "Two hotels closer than this, with very similar names, are the same hotel.",
        "used_by": _MATCH,
        "section": "hotels",
        "minimum": Decimal("10"),
        "maximum": Decimal("5000"),
    },
    {
        "key": MATCH_WARN_PCT,
        "type": "integer",
        "unit": "%",
        "question": "S285",
        "group": "pricing",
        "label": "Possible duplicate hotel: name similarity (%)",
        "help": "A new hotel whose cleaned name is at least this similar to one at the same place asks for a reason "
        "before it is saved as a different hotel.",
        "used_by": _MATCH,
        "section": "hotels",
        "minimum": Decimal("30"),
        "maximum": Decimal("100"),
    },
    {
        "key": MATCH_WARN_M,
        "type": "integer",
        "unit": "metres",
        "question": "S285",
        "group": "pricing",
        "label": "Possible duplicate hotel: distance (metres)",
        "help": "A hotel at the same place and closer than this asks for a reason before it is saved as a different "
        "hotel.",
        "used_by": _MATCH,
        "section": "hotels",
        "minimum": Decimal("10"),
        "maximum": Decimal("20000"),
    },
    {
        "key": FACTS_RECONFIRM_DAYS,
        "type": "integer",
        "unit": "days",
        "question": "S285",
        "group": "pricing",
        "label": "Re-confirm hotel facts after (days)",
        "help": "The facts of a hotel are due for a new confirmation this many days after they were last confirmed.",
        "used_by": _MATCH,
        "section": "hotels",
        "minimum": Decimal("30"),
        "maximum": Decimal("730"),
    },
    {
        "key": JUMP_WARN_PCT,
        "type": "integer",
        "unit": "%",
        "question": "S285",
        "group": "pricing",
        "label": "Rate sheet: price rise flagged (%)",
        "help": "A rate-sheet price at least this much above the same rate of the supplier's previous contract is "
        "flagged before the contract is published.",
        "used_by": "app.supply.rate_sheet",
        "section": "hotels",
        "minimum": Decimal("5"),
        "maximum": Decimal("500"),
    },
    {
        "key": DEFAULT_BOARD,
        "type": "code",
        "unit": None,
        "question": "S285",
        "group": "pricing",
        "label": "Default board of a hotel night",
        "help": "The board a hotel night is priced on when neither the quote line nor the tour night names one.",
        "used_by": "app.supply.rates",
        "section": "hotels",
        "choices": BOARDS,
    },
    {
        "key": RELEASE_DAYS_DEFAULT,
        "type": "integer",
        "unit": "days",
        "question": "S285",
        "group": "pricing",
        "label": "Allotment: release days when the contract gives none",
        "help": "Rooms or seats of an allotment not sold this many days before the date go back to the supplier, "
        "when the allotment term or the rate sheet names no release days.",
        "used_by": "app.supply.restrictions",
        "section": "hotels",
        "minimum": Decimal("0"),
        "maximum": Decimal("120"),
    },
    {
        "key": SHEET_MAX_ROWS,
        "type": "integer",
        "unit": "rows",
        "question": "S285",
        "group": "pricing",
        "label": "Rate sheet: most rows in one workbook",
        "help": "The most rows one rate-sheet workbook may hold on import (rates, seasons, hotel children and "
        "inventory together) and on export.",
        "used_by": "app.supply.rate_sheet",
        "section": "hotels",
        "minimum": Decimal("100"),
        "maximum": Decimal("5000"),
    },
)
