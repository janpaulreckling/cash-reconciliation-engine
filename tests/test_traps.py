"""False-positive traps: situations where a naive matcher applies cash to the wrong
invoice. Each must end in the right match or in review, never in a wrong auto-match."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from recon.matching.config import MatchConfig
from recon.matching.engine import Cash, Invoice, Matcher
from recon.matching.payouts import Payout, match_payouts

CFG = MatchConfig.load(Path(__file__).parents[1] / "config" / "matching.yaml")
D = date(2026, 11, 10)


def inv(n: int, cents: int, partner: str, pid: int) -> Invoice:
    return Invoice(f"INV/2026/{n:05d}", pid, partner, date(2026, 10, 20),
                   date(2026, 11, 19), cents)


def bank(id_: str, cents: int, memo: str, day: date = D) -> Cash:
    return Cash("bank_receipt", id_, day, cents, cents, None, memo, None)


# (a) similar customer names, same amount -------------------------------------
def test_similar_names_pick_the_named_customer() -> None:
    invoices = [inv(1, 50_000, "Blue Ridge Outfitters", 1),
                inv(2, 50_000, "Blue Ridge Outdoor Supply", 2)]
    [d] = Matcher(CFG, invoices).run([bank("b", 50_000, "ACH CREDIT BLUE RIDGE OUTDOOR SUPPLY")])
    assert d.invoices in ([], ["INV/2026/00002"])  # right one, or review; never the other


def test_ambiguous_partial_name_goes_to_review() -> None:
    invoices = [inv(1, 50_000, "Blue Ridge Outfitters", 1),
                inv(2, 50_000, "Blue Ridge Outdoor Supply", 2)]
    [d] = Matcher(CFG, invoices).run([bank("b", 50_000, "ACH CREDIT BLUE RIDGE")])
    assert d.tier is None and d.exception is not None


# (b) amount fits one invoice, the reference names another ---------------------
def test_conflicting_reference_goes_to_review() -> None:
    invoices = [inv(1, 20_000, "Lone Star Basecamp", 1), inv(2, 35_000, "Lone Star Basecamp", 1)]
    [d] = Matcher(CFG, invoices).run(
        [bank("b", 35_000, "ACH CREDIT LONE STAR BASECAMP INV/2026/00001")])
    assert d.tier is None and d.exception == "REFERENCE_CONFLICT"
    assert set(d.suggestion) == {"INV/2026/00001", "INV/2026/00002"}


# (d) refunds and disputes never become incoming cash ----------------------------
def test_refunded_or_disputed_card_payment_applies_nothing() -> None:
    m = Matcher(CFG, [inv(3, 20_000, "Sierra Summit Sports", 1)])
    refunded = Cash("stripe_charge", "ch_r", D, 20_000, 0, "INV/2026/00003", "", None,
                    frozenset({"REFUNDED"}))
    disputed = Cash("stripe_charge", "ch_d", D, 20_000, 0, "INV/2026/00003", "", None,
                    frozenset({"DISPUTED"}))
    decisions = m.run([refunded, disputed])
    assert all(sum(d.applied_cents) == 0 for d in decisions)
    assert m.remaining["INV/2026/00003"] == 20_000


# (e) several subsets add up to the same total ---------------------------------
def test_several_subsets_with_the_same_total_are_ambiguous() -> None:
    invoices = [inv(1, 10_000, "Hudson Valley Camping Co.", 1),
                inv(2, 20_000, "Hudson Valley Camping Co.", 1),
                inv(3, 30_000, "Hudson Valley Camping Co.", 1),
                inv(4, 45_000, "Hudson Valley Camping Co.", 1)]
    # 10k + 20k = 30k and 30k alone is single-invoice; 10k+20k+30k = 60k vs. ... use 30k:
    [d] = Matcher(CFG, invoices).run([bank("b", 30_000 + 45_000,
                                           "WIRE IN HUDSON VALLEY CAMPING CO")])
    # 30k+45k = 75k, and 10k+20k+45k = 75k: two subsets, so nobody may pick one
    assert d.tier is None and d.exception == "AMBIGUOUS"


# 2) subset search is bounded and falls back to review --------------------------
def test_subset_search_is_bounded_and_falls_back_to_review() -> None:
    import time

    # 80 open invoices of one customer: up to 6 of 80 is ~300 million combinations.
    invoices = [inv(i, 10_000 + i * 7, "Lone Star Basecamp", 1) for i in range(1, 81)]
    target = sum(i.residual_cents for i in invoices[:5]) + 3  # matches nothing exactly
    start = time.monotonic()
    [d] = Matcher(CFG, invoices).run([bank("b", target, "WIRE IN LONE STAR BASECAMP")])
    assert time.monotonic() - start < 2.0  # bounded, no hang
    assert d.tier is None and d.exception is not None  # and no guess


# (g) month-end cutoff: paid in November, deposited in December ------------------
def test_payout_arriving_after_month_end_is_not_flagged_before_its_window() -> None:
    p = Payout("po_dec", 90_000, date(2026, 12, 2), "paid", True, None, 3, 90_000)
    assert match_payouts([p], [], 3, today=date(2026, 12, 3)) == []


# (f) property: a cash item is decided exactly once; no invoice is over-applied --
@settings(max_examples=150, deadline=None)
@given(
    amounts=st.lists(st.integers(min_value=100, max_value=50_000), min_size=1, max_size=8),
    payments=st.lists(st.tuples(st.integers(0, 7), st.integers(-200, 200), st.booleans()),
                      min_size=1, max_size=10),
)
def test_no_cash_used_twice_and_no_invoice_over_applied(
        amounts: list[int], payments: list[tuple[int, int, bool]]) -> None:
    invoices = [inv(i + 1, a, f"Customer {i % 3}", i % 3) for i, a in enumerate(amounts)]
    cash = []
    for k, (idx, delta, cite) in enumerate(payments):
        target = invoices[idx % len(invoices)]
        memo = f"ACH CREDIT {target.partner_name.upper()}" + (f" {target.number}" if cite else "")
        cash.append(bank(f"b{k}", max(1, target.residual_cents + delta), memo))
    m = Matcher(CFG, invoices)
    decisions = m.run(cash)
    assert sorted(d.source_id for d in decisions) == sorted(c.id for c in cash)
    applied: dict[str, int] = {}
    for d in decisions:
        for n, a in zip(d.invoices, d.applied_cents, strict=True):
            applied[n] = applied.get(n, 0) + a + d.writeoff_cents
    for i in invoices:
        assert applied.get(i.number, 0) <= i.residual_cents
        assert m.remaining[i.number] >= 0


# O1: same amount, no reference: the date may rank suggestions but never decide ---
def test_same_amount_without_reference_is_ambiguous_even_if_dates_differ() -> None:
    # The twin is dated after the payment. Customers do pay before the invoice date
    # (the holdout has one), so the date may rank the suggestions but not decide.
    paid = Invoice("INV/2026/00001", 1, "Hudson Valley Camping Co.", date(2026, 11, 2),
                   date(2026, 12, 2), 157_872)
    twin = Invoice("INV/2026/00002", 1, "Hudson Valley Camping Co.", date(2026, 11, 20),
                   date(2026, 12, 20), 157_872)
    [d] = Matcher(CFG, [paid, twin]).run(
        [bank("b", 157_872, "ACH CREDIT HUDSON VALLEY CAMPING CO.")])
    assert d.tier is None and d.exception == "AMBIGUOUS"
    assert d.suggestion[0] == "INV/2026/00001"  # better date fit is ranked first
