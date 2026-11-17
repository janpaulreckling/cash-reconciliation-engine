"""Scenario plan for the November close: which open invoice gets paid how.

The plan is a pure function of the open invoices and a seed, so it is reproducible
and testable. Executors (Stripe now, Plaid next) turn each case into real API calls,
and `expected_matches()` turns the same plan into ground truth for measuring the
matching engine (precision/recall per tier).

Mix (user decision 2026-11-17): ~60% card via Stripe, ~30% bank transfer, ~10% open.
Dirty cases are assigned first so each one exists exactly once, regardless of seed.
"""

from __future__ import annotations

import random
import re
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum


class Channel(StrEnum):
    CARD = "card"
    TRANSFER = "transfer"
    OPEN = "open"


class Case(StrEnum):
    # card (Stripe)
    CARD_EXACT = "card_exact"                    # metadata ref + exact amount -> tier 1
    CARD_NO_METADATA = "card_no_metadata"        # ref only in a sloppy description -> tier 2
    CARD_PARTIAL = "card_partial"                # pays half -> invoice stays partially open
    CARD_REFUNDED = "card_refunded"              # paid, then fully refunded -> invoice open again
    CARD_PARTIAL_REFUND = "card_partial_refund"  # paid, then partly refunded -> short-paid
    CARD_DISPUTED = "card_disputed"              # paid, then disputed -> funds withdrawn
    CARD_DUPLICATE = "card_duplicate"            # same invoice paid twice -> one is a duplicate
    # transfer (bank, Plaid)
    TRANSFER_EXACT = "transfer_exact"            # memo has invoice number, exact amount -> tier 1
    TRANSFER_TYPO = "transfer_typo"              # mangled invoice number -> tier 2
    TRANSFER_NO_REF = "transfer_no_ref"          # only the customer name -> tier 2 (amount + name)
    TRANSFER_SHORT_FEE = "transfer_short_fee"    # bank fee deducted (<= $1) -> tier 2
    TRANSFER_SHORT_BIG = "transfer_short_big"    # $25 short -> exception AMOUNT_MISMATCH
    TRANSFER_OVERPAY = "transfer_overpay"        # pays $50 too much -> exception OVERPAYMENT
    TRANSFER_BATCH = "transfer_batch"            # one wire pays 3 invoices -> tier 3
    # nothing arrives
    OPEN = "open"
    # holdout only: patterns the engine was never tuned on (see seed/holdout.py)
    CARD_BILLING_NAME_ONLY = "card_billing_name_only"    # no metadata, no description
    TRANSFER_TRANSPOSED = "transfer_transposed"          # two digits swapped in the ref
    TRANSFER_WRONG_REF = "transfer_wrong_ref"            # cites another invoice of theirs
    TRANSFER_ABBREV_PAYER = "transfer_abbrev_payer"      # 'GT LAKES ADV GEAR', no ref
    TRANSFER_PO_REF = "transfer_po_ref"                  # their PO number, not our invoice
    TRANSFER_SAME_AMOUNT = "transfer_same_amount"        # two open invoices, same amount
    TRANSFER_TWO_NO_REF = "transfer_two_no_ref"          # pays two invoices, cites none
    TRANSFER_EARLY = "transfer_early"                    # bank date before invoice date
    TRANSFER_THIRD_PARTY = "transfer_third_party"        # parent company pays


# Expected outcome per case: (tier that should match it, exception code or None).
EXPECTED: dict[Case, tuple[int | None, str | None]] = {
    Case.CARD_EXACT: (1, None),
    Case.CARD_NO_METADATA: (2, None),
    Case.CARD_PARTIAL: (1, "PARTIAL_PAYMENT"),
    Case.CARD_REFUNDED: (1, "REFUNDED"),
    Case.CARD_PARTIAL_REFUND: (1, "PARTIAL_REFUND"),
    Case.CARD_DISPUTED: (1, "DISPUTED"),
    Case.CARD_DUPLICATE: (1, "DUPLICATE_PAYMENT"),
    Case.TRANSFER_EXACT: (1, None),
    Case.TRANSFER_TYPO: (2, None),
    Case.TRANSFER_NO_REF: (2, None),
    Case.TRANSFER_SHORT_FEE: (2, None),
    Case.TRANSFER_SHORT_BIG: (None, "AMOUNT_MISMATCH"),
    Case.TRANSFER_OVERPAY: (None, "OVERPAYMENT"),
    Case.TRANSFER_BATCH: (3, None),
    Case.OPEN: (None, None),
    # Holdout expectations are what an AR clerk would do, written before running the
    # engine on them. "REVIEW" = must not be auto-applied; any exception code is fine.
    Case.CARD_BILLING_NAME_ONLY: (2, None),
    Case.TRANSFER_TRANSPOSED: (2, None),
    Case.TRANSFER_WRONG_REF: (None, "REVIEW"),
    Case.TRANSFER_ABBREV_PAYER: (2, None),
    Case.TRANSFER_PO_REF: (2, None),
    Case.TRANSFER_SAME_AMOUNT: (None, "REVIEW"),
    Case.TRANSFER_TWO_NO_REF: (None, "REVIEW"),
    Case.TRANSFER_EARLY: (1, None),
    Case.TRANSFER_THIRD_PARTY: (1, None),
}

DIRTY_CARD = [Case.CARD_NO_METADATA, Case.CARD_NO_METADATA, Case.CARD_PARTIAL,
              Case.CARD_REFUNDED, Case.CARD_PARTIAL_REFUND, Case.CARD_DISPUTED,
              Case.CARD_DUPLICATE]
DIRTY_TRANSFER = [Case.TRANSFER_TYPO, Case.TRANSFER_TYPO, Case.TRANSFER_NO_REF,
                  Case.TRANSFER_SHORT_FEE, Case.TRANSFER_SHORT_BIG, Case.TRANSFER_OVERPAY]


@dataclass(frozen=True)
class OpenInvoice:
    id: int
    name: str
    partner_id: int
    partner_name: str
    opportunity_id: str | None
    residual_cents: int


@dataclass
class Scenario:
    case_id: str
    case: Case
    channel: Channel
    invoices: list[OpenInvoice]
    pay_cents: int                      # what the customer sends
    memo: str | None = None             # bank memo / card description
    extra: dict[str, int] = field(default_factory=dict)  # e.g. refund_cents, day_offset
    payer: str | None = None            # who pays, if not the invoiced customer

    @property
    def invoice_names(self) -> list[str]:
        return [i.name for i in self.invoices]


def card_fee_cents(amount_cents: int) -> int:
    """Stripe's standard US card pricing, 2.9% + 30c, rounded half up."""
    return int((Decimal(amount_cents) * Decimal("0.029")).quantize(Decimal(1))) + 30


def mangle(invoice_name: str, rng: random.Random) -> str:
    """How customers actually type invoice numbers into a bank memo."""
    m = re.fullmatch(r"INV/(\d{4})/(\d{5})", invoice_name)
    if not m:
        return invoice_name.lower()
    year, num = m.groups()
    short = str(int(num))
    variants = [f"INV {year} {short}", f"inv-{year}-{num}", f"INV{year}{num[:-1]}",
                f"Invoice {short}"]
    return rng.choice(variants)


def build_plan(invoices: list[OpenInvoice], seed: int = 2026) -> list[Scenario]:
    rng = random.Random(seed)
    pool = sorted(invoices, key=lambda i: i.name)
    rng.shuffle(pool)
    plan: list[Scenario] = []

    # Tier 3: one wire for three invoices of the customer with the most open items.
    by_partner: dict[int, list[OpenInvoice]] = defaultdict(list)
    for inv in pool:
        by_partner[inv.partner_id].append(inv)
    batch_partner = max(by_partner, key=lambda p: (len(by_partner[p]), -p))
    batch = by_partner[batch_partner][:3]
    if len(batch) == 3:
        plan.append(Scenario("S01", Case.TRANSFER_BATCH, Channel.TRANSFER, batch,
                             sum(i.residual_cents for i in batch),
                             memo=f"{batch[0].partner_name} payment " +
                                  " ".join(i.name for i in batch)))
        pool = [i for i in pool if i not in batch]

    n_open = max(1, round(len(invoices) * 0.10))
    n_transfer = max(len(DIRTY_TRANSFER) + 1, round(len(invoices) * 0.30) - len(batch))
    cases: list[tuple[Case, Channel]] = (
        [(c, Channel.TRANSFER) for c in DIRTY_TRANSFER]
        + [(Case.TRANSFER_EXACT, Channel.TRANSFER)] * (n_transfer - len(DIRTY_TRANSFER))
        + [(Case.OPEN, Channel.OPEN)] * n_open
        + [(c, Channel.CARD) for c in DIRTY_CARD]
    )
    cases += [(Case.CARD_EXACT, Channel.CARD)] * max(0, len(pool) - len(cases))

    for n, (inv, (case, channel)) in enumerate(zip(pool, cases, strict=False), start=len(plan) + 1):
        amount = inv.residual_cents
        memo: str | None = inv.name
        extra: dict[str, int] = {}
        if case is Case.CARD_NO_METADATA:
            memo = f"Order payment {mangle(inv.name, rng)}"
        elif case is Case.CARD_PARTIAL:
            amount = amount // 2
        elif case is Case.CARD_PARTIAL_REFUND:
            extra["refund_cents"] = max(100, amount // 10)
        elif case is Case.TRANSFER_TYPO:
            memo = mangle(inv.name, rng)
        elif case is Case.TRANSFER_NO_REF:
            memo = inv.partner_name.upper()
        elif case is Case.TRANSFER_SHORT_FEE:
            amount -= 75
        elif case is Case.TRANSFER_SHORT_BIG:
            amount -= 2500
        elif case is Case.TRANSFER_OVERPAY:
            amount += 5000
        elif case is Case.OPEN:
            memo, amount = None, 0
        plan.append(Scenario(f"S{n:02d}", case, channel, [inv], amount, memo, extra))
    return plan


def is_format_variant(invoice_name: str, memo: str | None) -> bool:
    """True if the memo still contains the invoice number as a standalone number (and no
    conflicting year), i.e. only the formatting differs: 'INV 2026 82', 'inv-2026-00082',
    'Invoice 82'. False for real typos such as a dropped digit ('INV20260008').
    Deliberately string-level, independent of the matching engine's normalizer."""
    m = re.fullmatch(r"INV/(\d{4})/(\d{5})", invoice_name)
    if not m or not memo:
        return False
    numbers = {int(n) for n in re.findall(r"\d+", memo)}
    year, num = int(m.group(1)), int(m.group(2))
    years = {n for n in numbers if 2000 <= n <= 2099}
    return num in numbers and (not years or year in years)


def expected_tier(s: Scenario) -> tuple[int | None, str | None]:
    tier, exception = EXPECTED[s.case]
    # A mangled reference that is only reformatted is an exact match after
    # normalization (tier 1, by design); a real typo needs the fuzzy tier.
    if s.case in (Case.TRANSFER_TYPO, Case.CARD_NO_METADATA) and \
            is_format_variant(s.invoices[0].name, s.memo):
        tier = 1
    return tier, exception


def expected_matches(plan: list[Scenario]) -> list[dict[str, str]]:
    """Ground truth rows: one per (scenario, invoice)."""
    rows = []
    for s in plan:
        tier, exception = expected_tier(s)
        for inv in s.invoices:
            rows.append({
                "case_id": s.case_id, "case": s.case.value, "channel": s.channel.value,
                "invoice": inv.name, "pay_cents": str(s.pay_cents),
                "expected_tier": "" if tier is None else str(tier),
                "expected_exception": exception or "",
            })
    return rows
