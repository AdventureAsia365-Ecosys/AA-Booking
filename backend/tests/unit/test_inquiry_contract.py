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

# Byte copies of AA-Ecosys docs/contracts (root commit fdab1eb). Update both together.
VENDORED_SHA256 = {
    "inquiry.submitted.v1.json": "22cbb36608398322b38da93dcf814f841da91f3a307ac9a9736df90d7f93e995",
    "inquiry.submitted.v1.1.json": "148ccaac54a13316f7f3ab7ce59458534a585968edf3cb5ed2e6b23bbb369f98",
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
    ],
    ids=["v1", "v1.1", "tripplanner-today", "legacy-guest"],
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
