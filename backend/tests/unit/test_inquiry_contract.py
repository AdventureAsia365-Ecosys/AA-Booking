"""inquiry.submitted validation and normalisation (contract rule 7: the consumer parses samples)."""

from __future__ import annotations

import hashlib
from datetime import datetime

import pytest

from app.contracts import CONTRACTS_DIR, load
from app.inquiry import contract
from app.inquiry.contract import ContractError, parse, validate, version_label
from app.inquiry.intake import _ts
from tests import inquiry_samples as samples

# Byte copies of AA-Ecosys docs/contracts (v1 / v1.1: root commit fdab1eb; v1.2: AA-830; v1.3: AA-986 L6). Update
# both together.
VENDORED_SHA256 = {
    "inquiry.submitted.v1.json": "22cbb36608398322b38da93dcf814f841da91f3a307ac9a9736df90d7f93e995",
    "inquiry.submitted.v1.1.json": "148ccaac54a13316f7f3ab7ce59458534a585968edf3cb5ed2e6b23bbb369f98",
    "inquiry.submitted.v1.2.json": "4ab794659d59ad1f764f82405013c37f9a968511d1873ea8fd09d8fa4eb76a8e",
    "inquiry.submitted.v1.3.json": "6ddd01eac5e84e5068a65cfa2ae24a73ef593b192e7190bc708551df828b1074",
}


@pytest.mark.parametrize(("name", "digest"), sorted(VENDORED_SHA256.items()))
def test_vendored_contracts_are_unchanged_copies(name: str, digest: str) -> None:
    assert hashlib.sha256((CONTRACTS_DIR / name).read_bytes()).hexdigest() == digest


def test_load_parses_the_schema() -> None:
    assert load("inquiry.submitted.v1.1")["title"].startswith("inquiry.submitted v1.1")


@pytest.mark.parametrize(
    ("payload", "label"),
    [
        (samples.v1(), "1"),
        (samples.v1_1(), "1.1"),
        (samples.v1_1_as_tripplanner_sends(), "1"),
        (samples.legacy_guest(), "1"),
        (samples.v1_2(), "1.2"),
        (samples.v1_2(keys=[None, None]), "1.2"),
    ],
    ids=["v1", "v1.1", "v1.1-labelled-1", "legacy-guest", "v1.2", "v1.2-unresolved"],
)
def test_accepted_samples(payload: dict, label: str) -> None:
    assert validate(payload) == label


@pytest.mark.parametrize(
    "payload",
    [
        samples.with_changes(samples.v1_1(), version=2),
        samples.with_changes(samples.v1_1(), version="1.1"),
        samples.with_changes(samples.v1_1(), version=True),
        samples.with_changes(samples.v1_1(), source="cis"),
        samples.with_changes(samples.v1_1(), itinerary={"legs": [], "days": []}),
        samples.with_changes(samples.v1_1(), extra="field"),
        samples.with_changes(samples.v1(), customer={"name": "x"}),
        [],
    ],
    ids=[
        "major-2",
        "string-version",
        "bool-version",
        "wrong-source",
        "missing-custom-days",
        "extra",
        "bad-customer",
        "not-object",
    ],
)
def test_rejected_payloads(payload: object) -> None:
    with pytest.raises(ContractError):
        validate(payload)


def test_v1_1_requires_verified() -> None:
    payload = samples.v1_1()
    del payload["customer"]["verified"]
    with pytest.raises(ContractError, match="customer"):
        validate(payload)


def test_version_label() -> None:
    assert version_label({"version": 1}) == "1"
    assert version_label({"version": 1.0}) == "1"
    assert version_label({"version": 1.1}) == "1.1"
    assert version_label({}) is None
    assert version_label("x") is None


def test_parse_normalises_contact_and_sorts_legs() -> None:
    payload = samples.v1_1("  Someone@Example.TEST ", verified=False)
    payload["customer"]["phone"] = "   "
    inquiry = parse(payload)
    assert inquiry.version == "1.1"
    assert inquiry.contact is not None
    assert inquiry.contact.email == "someone@example.test"
    assert inquiry.contact.phone is None
    assert inquiry.contact.verified is False
    assert inquiry.contact.external_ref is None
    assert [leg.position for leg in inquiry.legs] == [1, 2]
    assert inquiry.snapshot["contract_version"] == "1.1"
    assert inquiry.idempotency_key == f"{inquiry.trip_id}:{inquiry.handoff_id}"


def test_parse_v1_without_verified_is_unverified() -> None:
    payload = samples.v1()
    inquiry = parse(payload)
    assert inquiry.contact is not None and inquiry.contact.verified is False
    assert inquiry.contact.external_ref == payload["customer"]["customer_id"]


def test_parse_legacy_guest_has_no_contact() -> None:
    assert parse(samples.legacy_guest()).contact is None


def test_constants() -> None:
    assert (contract.CONTRACT, contract.SOURCE) == ("inquiry.submitted", "tripplanner")


def test_timestamp_parsing() -> None:
    assert _ts(None) is None
    assert _ts("not a date") is None
    assert _ts("2026-10-14T02:59:00Z") == datetime.fromisoformat("2026-10-14T02:59:00+00:00")


def _v1_2_leg(**changes: object) -> dict:
    payload = samples.v1_2()
    payload["itinerary"]["legs"][0].update(changes)
    return payload


@pytest.mark.parametrize(
    "payload",
    [
        _v1_2_leg(source_id=None),
        _v1_2_leg(source_system=None),
        _v1_2_leg(source_system="manual"),
        _v1_2_leg(source_id=""),
        samples.with_changes(samples.v1_1(), version=1.2),
    ],
    ids=["half-null-id", "half-null-system", "other-system", "empty-id", "v1.1-shape-labelled-1.2"],
)
def test_rejected_v1_2_legs(payload: dict) -> None:
    with pytest.raises(ContractError):
        validate(payload)


def test_version_label_1_2() -> None:
    assert version_label({"version": 1.2}) == "1.2"
    assert contract.SUPPORTED_VERSIONS == ("1", "1.1", "1.2", "1.3")


def test_parse_v1_2_carries_the_tour_key_per_leg() -> None:
    payload = samples.v1_2(keys=["src-a", None])
    by_tour = {leg["source_tour_id"]: leg for leg in payload["itinerary"]["legs"]}
    inquiry = parse(payload)
    assert inquiry.version == "1.2"
    for leg in inquiry.legs:
        sent = by_tour[leg.source_tour_id]
        assert (leg.source_system, leg.source_id) == (sent["source_system"], sent["source_id"])
    assert {(leg.source_system, leg.source_id) for leg in inquiry.legs} == {("cis", "src-a"), (None, None)}


def test_parse_v1_1_legs_have_no_tour_key() -> None:
    assert all(leg.source_id is None and leg.source_system is None for leg in parse(samples.v1_1()).legs)


# --- AA-986 L6: v1.3 (available / reason per leg and day) ------------------------------------------------------------


@pytest.mark.parametrize("reason", [None, "retired", "paused", "stop_removed", "not_served"])
def test_v1_3_is_accepted(reason: str | None) -> None:
    assert validate(samples.v1_3(unavailable=reason)) == "1.3"


def test_v1_3_requires_the_flag_and_a_reason_when_unavailable() -> None:
    payload = samples.v1_3(unavailable="retired")
    del payload["itinerary"]["legs"][0]["available"]
    with pytest.raises(ContractError):
        validate(payload)
    payload = samples.v1_3(unavailable="retired")
    payload["itinerary"]["legs"][-1]["reason"] = None
    with pytest.raises(ContractError):
        validate(payload)
    with pytest.raises(ContractError):  # a v1.2 shape labelled 1.3
        validate(samples.with_changes(samples.v1_2(), version=1.3))


def test_parse_v1_3_carries_the_flag_per_leg() -> None:
    inquiry = parse(samples.v1_3(unavailable="retired"))
    assert inquiry.version == "1.3"
    served, gone = inquiry.legs[:-1], inquiry.legs[-1]
    assert all(leg.available and leg.reason is None for leg in served)
    assert (gone.available, gone.reason, gone.reason_detail) == (False, "retired", "source_deleted")
    assert gone.source_system == "cis" and gone.source_id
    assert [d["available"] for d in inquiry.snapshot["days"]] == [True, False]


def test_older_versions_read_as_available() -> None:
    assert all(leg.available and leg.reason is None for leg in parse(samples.v1_2()).legs)
