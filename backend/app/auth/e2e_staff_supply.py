"""Synthetic-supply checks of the Dev-only E2E writers, beside :mod:`app.auth.e2e_staff` (A10e-4, AA-1151 S289).

:mod:`app.auth.e2e_staff` holds the writer identities, their route allow-list and the synthetic-path SQL; this module
holds the per-body check of the activity catalogue import (:data:`app.auth.e2e_staff.ACTIVITY_SHEET_IMPORT`), kept out
of that file so it stays within the code-health size cap. Same logger, same refusal code.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from app.auth.e2e_staff import E2E_READ_ONLY, SYNTHETIC_SUPPLY_MARK, log

# Each imported row queues a paid CIS draft job, so an E2E session imports at most this many.
ACTIVITY_SHEET_MAX_ROWS = 5  # technical-constant: the Dev-only E2E guard (root ADR 0006a A10e-4), not a policy


def check_activity_sheet(writer_name: str, sheet: Any) -> None:
    """A10e-4: the activity import route's check for an E2E session, before any preview or write (the path's supplier
    is synthetic). 403 ``e2e_read_only`` past :data:`ACTIVITY_SHEET_MAX_ROWS` rows or unless every row's title starts
    with :data:`~app.auth.e2e_staff.SYNTHETIC_SUPPLY_MARK` and has no inclusions.
    ``sheet`` = ``activity_import.parse(upload)``."""
    for i, row in enumerate(getattr(sheet, "rows", None) or ()):
        title = getattr(row, "title", None)
        in_cap = i < ACTIVITY_SHEET_MAX_ROWS
        marked = in_cap and isinstance(title, str) and title.strip().startswith(SYNTHETIC_SUPPLY_MARK)
        if not marked or (getattr(row, "service", None) or {}).get("inclusions"):
            log.info("booking_e2e_writer_refused identity=%s reason=activity_row", writer_name)
            raise HTTPException(status_code=403, detail=E2E_READ_ONLY)
