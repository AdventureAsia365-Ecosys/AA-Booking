"""AA-1096 l23 (tech lead S279: "each setting needs an explanation or definition so I know when to change it"): every
pricing policy parameter on Pricing > Policy carries a complete operator's guide in English and Vietnamese.

The same rule as the platform settings registry (AA-CIS-App CONTEXT.md, "Settings registry: every key ships its
guide"): {"en": {what, raise, lower, when, example}, "vi": {...}}, each part a text of 10-800 characters, the Vietnamese
part translated (not a copy), no issue id in operator text, and the guide's keys are exactly the parameters. A new
parameter without a guide fails here: add it to app/pricing/policy_guides.py.
"""

from __future__ import annotations

import re
from datetime import date

import pytest

from app.auth.staff import Staff
from app.pricing import policy_admin, policy_guides, policy_params

PARTS = ("what", "raise", "lower", "when", "example")
ISSUE_ID = re.compile(r"\bAA-\d+")


def guide_problems(guide: object) -> list[str]:
    """Why ``guide`` is not complete ([] = complete). Pure."""
    if not isinstance(guide, dict):
        return ["no guide"]
    out = []
    for lang in ("en", "vi"):
        parts = guide.get(lang)
        if not isinstance(parts, dict) or set(parts) != set(PARTS):
            out.append(f"{lang}: the five parts {PARTS} are required")
            continue
        out.extend(f"{lang}.{p} not 10-800 characters" for p in PARTS if not 10 <= len(parts[p].strip()) <= 800)
        out.extend(f"{lang}.{p} names an issue id" for p in PARTS if ISSUE_ID.search(parts[p]))
    if not out:
        out.extend(f"vi.{p} is a copy of en.{p}" for p in PARTS if guide["en"][p].strip() == guide["vi"][p].strip())
    return out


def test_every_parameter_has_a_complete_guide_and_no_guide_is_orphaned():
    assert set(policy_guides.GUIDES) == set(policy_params.PARAMS)
    problems = {k: guide_problems(policy_guides.guide(k)) for k in policy_params.PARAMS}
    assert not {k: v for k, v in problems.items() if v}


@pytest.mark.asyncio
async def test_the_staff_page_listing_carries_the_guide(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_versions(*_args, **_kwargs):
        return []

    monkeypatch.setattr(policy_admin, "_versions", no_versions)
    staff = Staff("adm-1", None, "Admin One", "admin", True, None)
    listing = await policy_admin.list_params(None, staff, today=date(2026, 11, 13))  # type: ignore[arg-type]
    assert {i["key"]: i["guide"] for i in listing["items"]} == policy_guides.GUIDES
    assert policy_guides.guide("no_such_key") is None


@pytest.mark.parametrize(
    ("guide", "expected"),
    [
        (None, ["no guide"]),
        ({"en": {p: "long enough text" for p in PARTS}}, [f"vi: the five parts {PARTS} are required"]),
        ({"en": {p: "long enough text" for p in PARTS}, "vi": {p: "đủ dài để đọc" for p in PARTS}}, []),
        (
            {"en": {p: "long enough text" for p in PARTS}, "vi": {**{p: "đủ dài để đọc" for p in PARTS}, "when": "x"}},
            ["vi.when not 10-800 characters"],
        ),
        (
            {"en": {p: "long enough text" for p in PARTS}, "vi": {p: "long enough text" for p in PARTS}},
            [f"vi.{p} is a copy of en.{p}" for p in PARTS],
        ),
        (
            {
                "en": {**{p: "long enough text" for p in PARTS}, "what": "see AA-1 for it"},
                "vi": {p: "đủ dài" * 3 for p in PARTS},
            },
            ["en.what names an issue id"],
        ),
    ],
)
def test_guide_problems(guide, expected):
    assert guide_problems(guide) == expected


CHOICE_TYPES_EXCLUDED = ("integer", "decimal", "boolean", "month_day")
STRICT_LOOSE = re.compile(r"stricter|looser|chặt hơn|nới hơn", re.IGNORECASE)
TODAY = re.compile(r"^\s*(Today|Hiện là|Hiện đang)\b")


def test_list_and_map_guides_name_no_stricter_or_looser_side():
    """The page heads a list or map's two directions "If you add more or make it bigger" / "If you remove some or make
    it smaller" (lib/policyGuide.ts directionWords "choice"): a "stricter / looser" word in the text would contradict
    the heading, since its sense depends on whose side you are on (review of the first pass)."""
    choice = [k for k, p in policy_params.PARAMS.items() if p.type not in CHOICE_TYPES_EXCLUDED]
    assert choice, "no list or map parameter left: the rule tests nothing"
    for key in choice:
        for lang in ("en", "vi"):
            for part in ("raise", "lower"):
                text = policy_guides.GUIDES[key][lang][part]
                assert not STRICT_LOOSE.search(text), f"{key}.{lang}.{part}: {text!r}"


def test_examples_give_the_starting_value_never_today():
    for key, guide in policy_guides.GUIDES.items():
        for lang in ("en", "vi"):
            assert not TODAY.search(guide[lang]["example"]), f"{key}.{lang}: {guide[lang]['example']!r}"
