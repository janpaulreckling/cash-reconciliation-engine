"""Holdout scenarios: payment patterns the matching engine was NOT designed or tuned on.

The November plan (plan.py) was written alongside the engine, so a perfect score on it
proves the plumbing, not generalization. This holdout runs on a separate batch of real
invoices with new patterns. Expected outcomes (plan.EXPECTED) were fixed before the
engine was run on them, from how an accounts-receivable clerk would treat each case;
the result is reported separately, misses included.
"""

from __future__ import annotations

import random
import re
from collections import defaultdict

from recon.seed.plan import Case, Channel, OpenInvoice, Scenario

ABBREVIATIONS = {"outfitters": "OUTF", "adventure": "ADV", "mountain": "MTN", "supply": "SUP",
                 "expeditions": "EXP", "provisions": "PROV", "camping": "CAMP", "great": "GT",
                 "company": "", "co.": "", "gear": "GR", "trail": "TRL"}


def abbreviate(name: str) -> str:
    words = [ABBREVIATIONS.get(w.lower(), w.upper()) for w in name.split()]
    return " ".join(w for w in words if w)


def transpose(invoice_name: str) -> str:
    m = re.fullmatch(r"INV/(\d{4})/(\d{5})", invoice_name)
    assert m, invoice_name
    digits = m.group(2).lstrip("0") or "0"
    if len(digits) < 2 or digits[-1] == digits[-2]:
        return f"INV/{m.group(1)}/{int(digits) + 10:05d}"
    swapped = digits[:-2] + digits[-1] + digits[-2]
    return f"INV/{m.group(1)}/{int(swapped):05d}"


def build_holdout_plan(invoices: list[OpenInvoice], seed: int = 4242) -> list[Scenario]:
    rng = random.Random(seed)
    pool = sorted(invoices, key=lambda i: i.name)
    by_partner: dict[int, list[OpenInvoice]] = defaultdict(list)
    for inv in pool:
        by_partner[inv.partner_id].append(inv)
    used: set[str] = set()
    plan: list[Scenario] = []

    def take(inv: OpenInvoice) -> OpenInvoice:
        used.add(inv.name)
        return inv

    def add(case: Case, channel: Channel, invs: list[OpenInvoice], pay: int, memo: str | None,
            **kw: object) -> None:
        plan.append(Scenario(f"H{len(plan) + 1:02d}", case, channel, invs, pay, memo, **kw))  # type: ignore[arg-type]

    # Same customer, two open invoices of the same amount, one payment without a ref.
    for invs in by_partner.values():
        amounts: dict[int, list[OpenInvoice]] = defaultdict(list)
        for i in invs:
            amounts[i.residual_cents].append(i)
        pair = next((v for v in amounts.values() if len(v) >= 2), None)
        if pair:
            add(Case.TRANSFER_SAME_AMOUNT, Channel.TRANSFER, [take(pair[0])],
                pair[0].residual_cents, pair[0].partner_name.upper())
            used.add(pair[1].name)  # the twin stays open
            break

    # Customers with several open invoices drive the multi-invoice and wrong-ref cases.
    multi = sorted((p for p, v in by_partner.items()
                    if len([i for i in v if i.name not in used]) >= 3),
                   key=lambda p: -len(by_partner[p]))
    if multi:
        a, b = [i for i in by_partner[multi[0]] if i.name not in used][:2]
        add(Case.TRANSFER_TWO_NO_REF, Channel.TRANSFER, [take(a), take(b)],
            a.residual_cents + b.residual_cents, a.partner_name.upper())
    if len(multi) > 1:
        x, y = [i for i in by_partner[multi[1]] if i.name not in used][:2]
        add(Case.TRANSFER_WRONG_REF, Channel.TRANSFER, [take(x)], x.residual_cents, y.name)
        used.add(y.name)  # cited but not paid: stays open

    rest = [i for i in pool if i.name not in used]
    rng.shuffle(rest)
    singles = [
        (Case.TRANSFER_TRANSPOSED, Channel.TRANSFER),
        (Case.TRANSFER_TRANSPOSED, Channel.TRANSFER),
        (Case.TRANSFER_ABBREV_PAYER, Channel.TRANSFER),
        (Case.TRANSFER_PO_REF, Channel.TRANSFER),
        (Case.TRANSFER_EARLY, Channel.TRANSFER),
        (Case.TRANSFER_THIRD_PARTY, Channel.TRANSFER),
        (Case.TRANSFER_SHORT_FEE, Channel.TRANSFER),
        (Case.TRANSFER_SHORT_BIG, Channel.TRANSFER),
        (Case.TRANSFER_EXACT, Channel.TRANSFER),
        (Case.TRANSFER_EXACT, Channel.TRANSFER),
        (Case.CARD_BILLING_NAME_ONLY, Channel.CARD),
        (Case.CARD_BILLING_NAME_ONLY, Channel.CARD),
        (Case.CARD_EXACT, Channel.CARD),
        (Case.CARD_EXACT, Channel.CARD),
        (Case.CARD_EXACT, Channel.CARD),
        (Case.CARD_EXACT, Channel.CARD),
    ]
    for inv, (case, channel) in zip(rest, singles, strict=False):
        amount = inv.residual_cents
        memo: str | None = inv.name
        extra: dict[str, int] = {}
        payer: str | None = None
        if case is Case.TRANSFER_TRANSPOSED:
            memo = transpose(inv.name)
        elif case is Case.TRANSFER_ABBREV_PAYER:
            memo, payer = None, abbreviate(inv.partner_name)
        elif case is Case.TRANSFER_PO_REF:
            memo = f"PO {rng.randint(4000, 4999)}"
        elif case is Case.TRANSFER_EARLY:
            extra = {"day_offset": -1}
        elif case is Case.TRANSFER_THIRD_PARTY:
            payer = "NORTHWIND RETAIL HOLDINGS"
        elif case is Case.TRANSFER_SHORT_FEE:
            amount -= 40
        elif case is Case.TRANSFER_SHORT_BIG:
            amount -= 5000
        elif case is Case.CARD_BILLING_NAME_ONLY:
            memo = None
        add(case, channel, [take(inv)], amount, memo, extra=extra, payer=payer)
    return plan
