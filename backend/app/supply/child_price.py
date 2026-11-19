"""The child price rule B4 (AA-1145, bk-060): the net of a child on a per-person service.

In this order, the first that exists wins:

1. the supplier's **child rate** for the service in force that day (``supplier_rates.pax_type = 'child'``; with
   several age bands, the band reaching the oldest age, since a quote carries no ages);
2. the **contract's child %** of the adult net (``supplier_contract_terms.child_pct`` of the contract the adult rate
   comes from: per supplier, set by contracting on the draft, published by a second person);
3. the **policy's child % for the service type** (pricing policy ``child_price_pct_by_service_type``, Pricing >
   Policy, maker-checker; seeded 100 for every type), when it is below 100;
4. else the **adult net with an age note**: the child pays the adult price and the line's price note says so, with
   the age up to which a traveller is a child (policy ``b2b_child_age_max``).

Infants pay the supplier's infant rate when there is one, else nothing (as before bk-060). Rooms are priced per room
(the hotel child and extra-bed policy comes with the hotel slice); a trip plan (``tp:``) line keeps the pricing rule's
sell-side child % (``pricing_rules.child_pct``, AA-1075) and never takes this rule too.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

BASIS_RATE = "supplier_rate"
BASIS_CONTRACT = "contract_pct"
BASIS_POLICY = "policy_pct"
BASIS_ADULT = "adult_price"
HUNDRED = Decimal("100")
CENT = Decimal("0.01")


@dataclass(frozen=True)
class ChildNet:
    net: Decimal
    basis: str
    pct: Decimal | None = None
    age_max: int | None = None  # the child rate's band (supplier_rate) or the policy's child age (adult_price)

    def note(self) -> dict[str, Any]:
        """What the quote line's ``price_note.child`` records (not hashed: the price itself is)."""
        out: dict[str, Any] = {"basis": self.basis}
        if self.pct is not None:
            out["pct"] = format(self.pct.normalize(), "f")
        if self.age_max is not None:
            out["age_max"] = self.age_max
        return out


def child_net(
    adult_net: Decimal,
    *,
    child_rate_net: Decimal | None = None,
    child_rate_age_max: int | None = None,
    contract_pct: Decimal | None = None,
    policy_pct: Decimal | None = None,
    child_age_max: int | None = None,
) -> ChildNet:
    """The unit net of one child (see the module docstring for the order)."""
    if child_rate_net is not None:
        return ChildNet(child_rate_net, BASIS_RATE, age_max=child_rate_age_max)
    if contract_pct is not None:
        return ChildNet(_share(adult_net, contract_pct), BASIS_CONTRACT, pct=contract_pct)
    if policy_pct is not None and policy_pct < HUNDRED:
        return ChildNet(_share(adult_net, policy_pct), BASIS_POLICY, pct=policy_pct)
    return ChildNet(adult_net, BASIS_ADULT, age_max=child_age_max)


def _share(amount: Decimal, pct: Decimal) -> Decimal:
    return (amount * pct / HUNDRED).quantize(CENT, rounding=ROUND_HALF_UP)
