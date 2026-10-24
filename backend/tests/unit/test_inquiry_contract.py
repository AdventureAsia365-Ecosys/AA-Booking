"""inquiry.submitted validation and normalisation (contract rule 7: the consumer parses samples)."""

from __future__ import annotations

import dataclasses
import hashlib
from datetime import datetime

import pytest

from app.contracts import CONTRACTS_DIR, load
from app.inquiry import contract
from app.inquiry.contract import ContractError, parse, validate, version_label
from app.inquiry.intake import _ts
from tests import inquiry_samples as samples

# Byte copies of AA-Ecosys docs/contracts (v1 / v1.1: root commit fdab1eb; v1.2: AA-830; v1.3: AA-986 L6; v1.4: AA-908,
# root #237). Update both together.
VENDORED_SHA256 = {
    "inquiry.submitted.v1.json": "22cbb36608398322b38da93dcf814f841da91f3a307ac9a9736df90d7f93e995",
    "inquiry.submitted.v1.1.json": "148ccaac54a13316f7f3ab7ce59458534a585968edf3cb5ed2e6b23bbb369f98",
    "inquiry.submitted.v1.2.json": "4ab794659d59ad1f764f82405013c37f9a968511d1873ea8fd09d8fa4eb76a8e",
    "inquiry.submitted.v1.3.json": "6ddd01eac5e84e5068a65cfa2ae24a73ef593b192e7190bc708551df828b1074",
    "inquiry.submitted.v1.4.json": "655c7442e0cad471555fd7731fdc5a8a93c3a22a216b4aa96d64cdc4b7ffe810",
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
    assert contract.SUPPORTED_VERSIONS == ("1", "1.1", "1.2", "1.3", "1.4")


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
    # review round: the snapshot keeps the leg flags too, by position (trip_case_legs has no column for them yet)
    snap_legs = inquiry.snapshot["legs"]
    assert [leg["position"] for leg in snap_legs] == [leg.position for leg in inquiry.legs]
    assert snap_legs[-1] == {
        "position": gone.position,
        "source_tour_id": gone.source_tour_id,
        "available": False,
        "reason": "retired",
        "reason_detail": "source_deleted",
    }
    assert all(leg["available"] and leg["reason"] is None for leg in snap_legs[:-1])


def test_older_versions_keep_the_snapshot_shape() -> None:
    assert "legs" not in parse(samples.v1_2()).snapshot


def test_older_versions_read_as_available() -> None:
    assert all(leg.available and leg.reason is None for leg in parse(samples.v1_2()).legs)


# --- AA-908: v1.4 (customer.identity of a signed-in traveller) -------------------------------------------------------


def test_v1_4_signed_in_and_guest_are_accepted() -> None:
    assert validate(samples.v1_4()) == "1.4"
    assert validate(samples.v1_4(signed_in=False)) == "1.4"


def test_v1_4_verified_needs_an_identity() -> None:
    payload = samples.v1_4()
    payload["customer"]["identity"] = None
    with pytest.raises(ContractError):
        validate(payload)


@pytest.mark.parametrize(
    "identity",
    [{"iss": samples.ISS}, {"iss": samples.ISS, "sub": ""}, {"iss": samples.ISS, "sub": "s", "email": "x@y.test"}],
    ids=["no-sub", "empty-sub", "extra-field"],
)
def test_v1_4_rejects_a_bad_identity(identity: dict) -> None:
    payload = samples.v1_4()
    payload["customer"]["identity"] = identity
    with pytest.raises(ContractError):
        validate(payload)


def test_v1_4_requires_the_identity_key_and_older_versions_refuse_it() -> None:
    payload = samples.v1_4(signed_in=False)
    del payload["customer"]["identity"]
    with pytest.raises(ContractError):
        validate(payload)
    older = samples.v1_3()
    older["customer"]["identity"] = {"iss": samples.ISS, "sub": "s"}
    with pytest.raises(ContractError):
        validate(older)


@pytest.mark.parametrize(
    "iss",
    ["http://evil.example/pool", "not a uri", "https://idp.example/ pool"],
    ids=["http", "not-a-uri", "whitespace"],
)
def test_v1_4_rejects_an_issuer_the_bk024_check_would_refuse(iss: str) -> None:
    """Review round 1: the schema's ``format: uri`` is not enforced, so the shape is checked in code (no poison
    message: a CheckViolation in intake would be retried; a ContractError fails the inbox row once)."""
    payload = samples.v1_4()
    payload["customer"]["identity"]["iss"] = iss
    with pytest.raises(ContractError, match="customer/identity/iss") as caught:
        validate(payload)
    assert iss not in str(caught.value)  # the reason is stored and logged: never the value (DM-6.4)


def test_v1_4_issuer_length_limit_is_300() -> None:
    payload = samples.v1_4()
    payload["customer"]["identity"]["iss"] = "https://" + "a" * 292
    assert validate(payload) == "1.4"
    payload["customer"]["identity"]["iss"] += "a"  # 301: refused by the schema's maxLength (under customer's oneOf)
    with pytest.raises(ContractError, match="v1.4 invalid: customer"):
        validate(payload)


def test_parse_pins_the_traveller_issuer() -> None:
    payload = samples.v1_4()
    payload["customer"]["identity"]["iss"] = samples.FOREIGN_ISS
    assert parse(payload).contact.identity.iss == samples.FOREIGN_ISS  # type: ignore[union-attr]  # no pin given
    with pytest.raises(ContractError, match="not the accepted traveller issuer") as caught:
        parse(payload, traveller_issuers={samples.ISS})
    assert samples.FOREIGN_ISS not in str(caught.value)
    assert parse(samples.v1_4(), traveller_issuers={samples.ISS}).contact.signed_in  # type: ignore[union-attr]
    # a guest send has no identity, so the pin does not apply
    assert parse(samples.v1_4(signed_in=False), traveller_issuers={samples.ISS}).contact.identity is None  # type: ignore[union-attr]


def test_parse_v1_4_signed_in_carries_the_identity() -> None:
    payload = samples.v1_4("  Traveller@Example.TEST ", sub="sub-1")
    inquiry = parse(payload)
    assert inquiry.version == "1.4"
    assert inquiry.contact is not None
    assert inquiry.contact.identity == contract.Identity(samples.ISS, "sub-1")
    assert inquiry.contact.verified is True and inquiry.contact.email == "traveller@example.test"
    assert inquiry.contact.signed_in is True
    # the identity is not copied into the trip case snapshot (it lives in booking.customer_identities)
    assert "identity" not in str(inquiry.snapshot) and "sub-1" not in str(inquiry.snapshot)
    assert inquiry.snapshot["legs"]  # v1.4 is additive over v1.3: the leg flags are kept


def test_parse_v1_4_guest_is_not_signed_in() -> None:
    contact = parse(samples.v1_4(signed_in=False)).contact
    assert contact is not None and contact.identity is None and contact.signed_in is False


def test_signed_in_needs_identity_verified_and_email() -> None:
    contact = parse(samples.v1_4()).contact
    assert contact is not None and contact.signed_in
    for changes in ({"verified": False}, {"identity": None}, {"email": None}):
        assert not dataclasses.replace(contact, **changes).signed_in, changes
    assert parse(samples.v1_3()).contact.identity is None  # type: ignore[union-attr]
