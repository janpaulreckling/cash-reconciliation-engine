"""Stripe payouts against bank deposits (A3) and the payout sum control (A2)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from recon.matching.engine import Decision


@dataclass(frozen=True)
class Payout:
    id: str
    amount_cents: int
    arrival_date: date
    status: str
    automatic: bool
    trace_id: str | None
    component_count: int
    components_net_cents: int


@dataclass(frozen=True)
class Deposit:
    id: str
    day: date
    amount_cents: int
    description: str


def match_payouts(payouts: list[Payout], deposits: list[Deposit], window_days: int,
                  today: date) -> list[Decision]:
    out: list[Decision] = []
    used: set[str] = set()
    for p in sorted(payouts, key=lambda p: p.arrival_date):
        if p.status != "paid":
            continue
        # A2: does the payout equal what Stripe swept into it?
        if not p.automatic or p.component_count == 0:
            out.append(Decision("stripe_payout", p.id, None, 0.0, [], [],
                                exception="PAYOUT_NO_COMPONENTS", rule="payout-sum"))
        elif p.components_net_cents != p.amount_cents:
            out.append(Decision("stripe_payout", p.id, None, 0.0, [], [],
                                exception="PAYOUT_SUM_BREAK", rule="payout-sum",
                                breakdown={"payout": p.amount_cents,
                                           "components": p.components_net_cents}))
        # A3: did it arrive in the bank?
        hits = [d for d in deposits if d.id not in used and d.amount_cents == p.amount_cents
                and abs((d.day - p.arrival_date).days) <= window_days]
        hits.sort(key=lambda d: (not (p.trace_id and p.trace_id in d.description),
                                 abs((d.day - p.arrival_date).days)))
        if hits:
            used.add(hits[0].id)
            dep = hits[0]
            traced = bool(p.trace_id and p.trace_id in dep.description)
            out.append(Decision("stripe_payout", p.id, 1, 1.0, [], [], rule="payout-deposit",
                                suggestion=[dep.id],
                                breakdown={"trace_id_match": float(traced),
                                           "days_late": float((dep.day - p.arrival_date).days)}))
        elif (today - p.arrival_date).days > window_days:
            out.append(Decision("stripe_payout", p.id, None, 0.0, [], [],
                                exception="PAYOUT_NOT_IN_BANK", rule="payout-deposit"))
    for d in deposits:
        if d.id not in used:
            out.append(Decision("bank_receipt", d.id, None, 0.0, [], [],
                                exception="DEPOSIT_WITHOUT_PAYOUT", rule="payout-deposit"))
    return out
