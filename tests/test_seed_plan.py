from collections import Counter

from recon.seed.plan import (
    DIRTY_CARD,
    DIRTY_TRANSFER,
    Case,
    Channel,
    OpenInvoice,
    build_plan,
    card_fee_cents,
    expected_matches,
    mangle,
)


def invoices(n: int = 42) -> list[OpenInvoice]:
    return [OpenInvoice(id=i, name=f"INV/2026/{i:05d}", partner_id=i % 9,
                        partner_name=f"Customer {i % 9}", opportunity_id=f"006{i:015d}",
                        residual_cents=100_000 + i * 1_337)
            for i in range(1, n + 1)]


def test_plan_is_reproducible() -> None:
    a = [(s.case, s.invoice_names, s.pay_cents) for s in build_plan(invoices())]
    b = [(s.case, s.invoice_names, s.pay_cents) for s in build_plan(invoices())]
    assert a == b


def test_every_invoice_is_used_exactly_once() -> None:
    names = [n for s in build_plan(invoices()) for n in s.invoice_names]
    assert sorted(names) == sorted(i.name for i in invoices())


def test_mix_is_about_60_30_10() -> None:
    per_invoice = Counter(s.channel for s in build_plan(invoices()) for _ in s.invoices)
    assert per_invoice[Channel.CARD] == 25
    assert per_invoice[Channel.TRANSFER] == 13
    assert per_invoice[Channel.OPEN] == 4


def test_every_dirty_case_is_present() -> None:
    cases = Counter(s.case for s in build_plan(invoices()))
    for case, n in (Counter(DIRTY_CARD) + Counter(DIRTY_TRANSFER)).items():
        assert cases[case] == n
    assert cases[Case.TRANSFER_BATCH] == 1


def test_batch_wire_pays_three_invoices_of_one_customer() -> None:
    [batch] = [s for s in build_plan(invoices()) if s.case is Case.TRANSFER_BATCH]
    assert len({i.partner_id for i in batch.invoices}) == 1
    assert batch.pay_cents == sum(i.residual_cents for i in batch.invoices)


def test_amount_deviations() -> None:
    by_case = {s.case: s for s in build_plan(invoices())}
    residual = {c: s.invoices[0].residual_cents for c, s in by_case.items()}
    assert by_case[Case.TRANSFER_SHORT_FEE].pay_cents == residual[Case.TRANSFER_SHORT_FEE] - 75
    assert by_case[Case.TRANSFER_SHORT_BIG].pay_cents == residual[Case.TRANSFER_SHORT_BIG] - 2500
    assert by_case[Case.TRANSFER_OVERPAY].pay_cents == residual[Case.TRANSFER_OVERPAY] + 5000
    assert by_case[Case.CARD_PARTIAL].pay_cents == residual[Case.CARD_PARTIAL] // 2


def test_typo_memos_no_longer_contain_the_exact_invoice_number() -> None:
    for s in build_plan(invoices()):
        if s.case in (Case.TRANSFER_TYPO, Case.CARD_NO_METADATA):
            assert s.memo is not None and s.invoices[0].name not in s.memo


def test_mangle_keeps_the_number_recognizable() -> None:
    import random
    out = mangle("INV/2026/00087", random.Random(1))
    assert "87" in out


def test_stripe_fee_is_2_9_percent_plus_30_cents() -> None:
    assert card_fee_cents(10_000) == 320
    assert card_fee_cents(123_456) == 3_610  # matches the fee Stripe charged in test mode


def test_ground_truth_has_one_row_per_invoice() -> None:
    plan = build_plan(invoices())
    rows = expected_matches(plan)
    assert len(rows) == 42
    batch_rows = [r for r in rows if r["case"] == "transfer_batch"]
    assert {r["expected_tier"] for r in batch_rows} == {"3"}
