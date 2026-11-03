"""AA-1040 (EU AI Act Art. 50(2)) — the machine-readable AI mark of the CIS supply text Booking shows.

CIS 358 appends ai_generated / ai_generated_at / ai_human_edited to catalog.v_supply_content. Booking detects the
three columns separately from the read-back's own nine (app.supply.content.MARK_VIEW): before 358 the content is
served as before with ``ai: null``; after it, a published row carries ``ai`` = {ai_generated, generator, model,
generated_at, human_edited}; an unpublished row never does. The hotel / service detail and the proposal items carry
it. The SQL against a stand-in view on PostgreSQL is tests/integration/test_bk023_media_api.py."""

from __future__ import annotations

from typing import Any

import pytest

from app.catalog import liveness
from app.supply import content
from app.trip_cases import versions
from tests.unit.fakes import VIEW_READY_FRAGMENT, ScriptedConn
from tests.unit.test_supply import HID, SID
from tests.unit.test_supply_content import CONTENT_MANY, CONTENT_ONE, ROOM, content_row, item, items_conn

GENERATED_AT = "2026-10-02T03:00:00+00:00"


@pytest.fixture(autouse=True)
def _fresh_detection():
    liveness.reset_cache()
    yield
    liveness.reset_cache()


def detect(marked: bool):
    """VIEW_READY answer: the read-back view is always there; the mark columns only when ``marked``."""

    def answer(_schema: str, _name: str, columns: list[str]) -> bool:
        return marked or "ai_generated" not in columns

    return answer


def conn_with(row: dict[str, Any], *, marked: bool = True) -> ScriptedConn:
    return ScriptedConn().on(VIEW_READY_FRAGMENT, detect(marked)).on(CONTENT_ONE, row)


def ai_row(generated: bool | None, edited: bool | None = None, status: str = "published") -> dict[str, Any]:
    return content_row(
        status,
        ai_generated=generated,
        ai_generated_at=GENERATED_AT if generated else None,
        ai_human_edited=edited,
    )


def test_mark_view_detects_only_the_three_columns() -> None:
    assert content.MARK_VIEW.qualified == content.CONTENT_VIEW.qualified == "catalog.v_supply_content"
    assert content.MARK_VIEW.columns == ("ai_generated", "ai_generated_at", "ai_human_edited")
    assert not set(content.MARK_VIEW.columns) & set(content.CONTENT_VIEW.columns)


@pytest.mark.asyncio
async def test_an_ai_draft_published_as_written() -> None:
    conn = conn_with(ai_row(True, False))
    out = await content.read_content(conn, "hotel", HID)
    assert out is not None and out["ai"] == {
        "ai_generated": True,
        "generator": content.GENERATOR,
        "model": None,
        "generated_at": GENERATED_AT,
        "human_edited": False,
    }
    sql = conn.calls[-1][0]
    assert "c.ai_generated" in sql and "ai_human_edited" in sql


@pytest.mark.asyncio
async def test_a_staff_edit_of_the_draft_and_a_text_written_from_scratch() -> None:
    edited = await content.read_content(conn_with(ai_row(True, True)), "hotel", HID)
    assert edited is not None and edited["ai"]["ai_generated"] is True and edited["ai"]["human_edited"] is True
    own = await content.read_content(conn_with(ai_row(False)), "hotel", HID)
    assert own is not None and own["ai"] == {
        "ai_generated": False,
        "generator": None,
        "model": None,
        "generated_at": None,
        "human_edited": None,
    }


@pytest.mark.parametrize("status", ["draft", "in_review", "stale", "unpublished"])
@pytest.mark.asyncio
async def test_unpublished_rows_carry_no_mark(status: str) -> None:
    out = await content.read_content(conn_with(ai_row(True, False, status)), "hotel", HID)
    assert out is not None and out["description"] is None and out["ai"] is None


@pytest.mark.asyncio
async def test_before_cis_358_the_content_is_served_without_the_mark() -> None:
    conn = conn_with(content_row(), marked=False)
    out = await content.read_content(conn, "hotel", HID)
    assert out is not None and out["description"] == "A quiet garden hotel." and out["ai"] is None
    assert "ai_generated" not in conn.calls[-1][0]  # the SELECT never names a column the view lacks
    assert content.EMPTY["ai"] is None


@pytest.mark.asyncio
async def test_proposal_items_carry_the_mark() -> None:
    rows = [
        item({**ROOM, "entity_type": "hotel", "entity_id": HID}),
        item({**ROOM, "entity_type": "hotel", "entity_id": SID}),
    ]
    conn = items_conn(rows).on(VIEW_READY_FRAGMENT, detect(True)).on(CONTENT_MANY, [ai_row(True, True)])
    out = await versions.get_items(conn, "TC-000001", 1)
    marked, unpublished = (i["supply"] for i in out["items"])
    assert marked["ai"]["ai_generated"] is True and marked["ai"]["human_edited"] is True
    assert marked["ai"]["generated_at"] == GENERATED_AT
    assert unpublished["ai"] is None and unpublished["description"] is None
