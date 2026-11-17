"""Matching engine rules, one test per decision the engine has to get right."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from recon.matching.config import MatchConfig
from recon.matching.engine import Cash, Invoice, Matcher
from recon.matching.payouts import Deposit, Payout, match_payouts
from recon.matching.text import name_score, ref_score, refs_in

CFG = MatchConfig.load(Path(__file__).parents[1] / "config" / "matching.yaml")
D = date(2026, 11, 10)


def inv(n: int, cents: int, partner: str = "Blue Ridge Outfitters", pid: int = 1,
        issued: date = date(2026, 10, 20), due: date = date(2026, 11, 19)) -> Invoice:
    return Invoice(f"INV/2026/{n:05d}", pid, partner, issued, due, cents)


def bank(id_: str, cents: int, memo: str, day: date = D) -> Cash:
    return Cash("bank_receipt", id_, day, cents, cents, None, memo, None)


def card(id_: str, cents: int, ref: str | None, desc: str = "", net: int | None = None,
         flags: frozenset[str] = frozenset()) -> Cash:
    return Cash("stripe_charge", id_, D, cents, cents if net is None else net, ref, desc,
                "Blue Ridge Outfitters", flags)


# --- text normalization ------------------------------------------------------
@pytest.mark.parametrize("memo", ["INV/2026/00087", "INV 2026 87", "inv-2026-00087",
                                  "Invoice 87", "payment INV#2026/87"])
def test_reference_formats_all_resolve_to_the_same_invoice(memo: str) -> None:
    assert ref_score("INV/2026/00087", memo) == 1.0


def test_dropped_digit_is_fuzzy_not_exact() -> None:
    assert 0.8 <= ref_score("INV/2026/00085", "INV20260008") < 1.0


def test_wrong_year_is_not_an_exact_reference() -> None:
    assert ref_score("INV/2026/00087", "INV 2025 87") < 1.0


def test_several_references_in_one_memo() -> None:
    assert refs_in("payment INV/2026/00099 INV/2026/00095") == {(2026, 99), (2026, 95)}


def test_customer_name_ignores_legal_suffix_and_bank_prefix() -> None:
    assert name_score("Cascade Paddle Co.", "ACH CREDIT CASCADE PADDLE CO") == 1.0


# --- tiers -------------------------------------------------------------------
def test_tier1_exact_reference_and_amount() -> None:
    [d] = Matcher(CFG, [inv(87, 50_000)]).run([bank("b1", 50_000, "ACH CREDIT X INV/2026/00087")])
    assert (d.tier, d.invoices, d.applied_cents, d.exception) == (1, ["INV/2026/00087"],
                                                                  [50_000], None)


def test_tier2_name_and_amount_without_reference() -> None:
    invoices = [inv(1, 50_000), inv(2, 70_000)]
    [d] = Matcher(CFG, invoices).run([bank("b1", 70_000, "ACH CREDIT BLUE RIDGE OUTFITTERS")])
    assert d.tier == 2 and d.invoices == ["INV/2026/00002"]
    assert d.breakdown["name"] == 1.0 and d.breakdown["amount"] == 1.0


def test_tier2_small_bank_fee_is_applied_and_written_off() -> None:
    [d] = Matcher(CFG, [inv(5, 100_000)]).run([bank("b1", 99_925, "INV/2026/00005")])
    assert d.tier == 2 and d.writeoff_cents == 75 and d.exception is None


def test_same_customer_same_amount_is_ambiguous_not_guessed() -> None:
    invoices = [inv(1, 50_000), inv(2, 50_000)]
    [d] = Matcher(CFG, invoices).run([bank("b1", 50_000, "ACH CREDIT BLUE RIDGE OUTFITTERS")])
    assert d.tier is None and d.exception == "AMBIGUOUS"
    assert set(d.suggestion) == {"INV/2026/00001", "INV/2026/00002"}


def test_tier3_one_wire_for_three_invoices() -> None:
    invoices = [inv(1, 10_000), inv(2, 20_000), inv(3, 30_000), inv(4, 45_000)]
    memo = "WIRE IN BLUE RIDGE OUTFITTERS INV/2026/00001 INV/2026/00002 INV/2026/00003"
    [d] = Matcher(CFG, invoices).run([bank("b1", 60_000, memo)])
    assert d.tier == 3 and sorted(d.invoices) == ["INV/2026/00001", "INV/2026/00002",
                                                  "INV/2026/00003"]


def test_tier3_without_references_goes_to_review() -> None:
    invoices = [inv(1, 10_000), inv(2, 20_000), inv(3, 45_000)]
    [d] = Matcher(CFG, invoices).run([bank("b1", 30_000, "WIRE IN BLUE RIDGE OUTFITTERS")])
    assert d.tier is None and d.exception == "LOW_CONFIDENCE"


# --- exceptions --------------------------------------------------------------
def test_big_short_payment_is_flagged_not_matched() -> None:
    [d] = Matcher(CFG, [inv(6, 100_000)]).run([bank("b1", 97_500, "INV/2026/00006")])
    assert d.tier is None and d.exception == "AMOUNT_MISMATCH"
    assert d.suggestion == ["INV/2026/00006"]


def test_overpayment_is_flagged() -> None:
    [d] = Matcher(CFG, [inv(7, 100_000)]).run([bank("b1", 105_000, "INV/2026/00007")])
    assert d.exception == "OVERPAYMENT"


def test_unknown_deposit_is_unapplied_cash() -> None:
    [d] = Matcher(CFG, [inv(8, 100_000)]).run([bank("b1", 1_250, "DEPOSIT REF 88213")])
    assert d.exception == "UNAPPLIED_CASH" and d.suggestion == []


def test_second_card_payment_for_a_paid_invoice_is_a_duplicate() -> None:
    m = Matcher(CFG, [inv(9, 20_000)])
    first, second = m.run([card("ch_a", 20_000, "INV/2026/00009"),
                           card("ch_b", 20_000, "INV/2026/00009")])
    assert first.exception is None and second.exception == "DUPLICATE_PAYMENT"
    assert second.applied_cents == [0]


def test_refunded_card_payment_matches_but_applies_nothing() -> None:
    [d] = Matcher(CFG, [inv(10, 20_000)]).run(
        [card("ch", 20_000, "INV/2026/00010", net=0, flags=frozenset({"REFUNDED"}))])
    assert (d.tier, d.exception, d.applied_cents) == (1, "REFUNDED", [0])


def test_partial_card_payment_leaves_the_rest_open() -> None:
    m = Matcher(CFG, [inv(11, 20_000)])
    [d] = m.run([card("ch", 10_000, "INV/2026/00011")])
    assert d.exception == "PARTIAL_PAYMENT" and m.remaining["INV/2026/00011"] == 10_000


def test_charge_for_an_unknown_invoice_has_no_candidate() -> None:
    [d] = Matcher(CFG, [inv(12, 20_000)]).run([card("ch", 50_000, "PROBE/0002")])
    assert d.exception == "NO_CANDIDATE"


def test_tier1_is_never_blocked_by_a_weaker_match_seen_earlier() -> None:
    # The fuzzy receipt comes first by date, but the exact one must win the invoice.
    invoices = [inv(1, 50_000)]
    early_fuzzy = bank("b0", 50_000, "ACH CREDIT BLUE RIDGE OUTFITTERS", day=date(2026, 11, 3))
    exact = bank("b1", 50_000, "INV/2026/00001", day=date(2026, 11, 9))
    by_id = {d.source_id: d for d in Matcher(CFG, invoices).run([early_fuzzy, exact])}
    assert by_id["b1"].tier == 1
    assert by_id["b0"].tier is None and by_id["b0"].exception == "DUPLICATE_PAYMENT"


# --- payouts -----------------------------------------------------------------
def test_payout_matches_deposit_and_passes_sum_control() -> None:
    p = Payout("po_1", 90_000, date(2026, 11, 19), "paid", True, "TR1", 3, 90_000)
    deps = [Deposit("d1", date(2026, 11, 20), 90_000, "STRIPE TRANSFER NORTHWIND TR1")]
    [d] = match_payouts([p], deps, 3, date(2026, 11, 25))
    assert d.tier == 1 and d.breakdown["trace_id_match"] == 1.0


def test_payout_sum_break_and_missing_deposit() -> None:
    p = Payout("po_1", 90_000, date(2026, 11, 10), "paid", True, None, 3, 89_000)
    codes = {d.exception for d in match_payouts([p], [], 3, date(2026, 11, 25))}
    assert codes == {"PAYOUT_SUM_BREAK", "PAYOUT_NOT_IN_BANK"}


def test_stripe_deposit_without_payout_is_flagged() -> None:
    [d] = match_payouts([], [Deposit("d1", D, 5_000, "STRIPE TRANSFER")], 3, D)
    assert d.exception == "DEPOSIT_WITHOUT_PAYOUT"
