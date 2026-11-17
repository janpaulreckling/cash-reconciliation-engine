from collections import Counter

from recon.seed.holdout import abbreviate, build_holdout_plan, transpose
from recon.seed.plan import EXPECTED, Case, OpenInvoice


def invoices() -> list[OpenInvoice]:
    out = []
    n = 200
    for pid, name, amounts in [(1, "Great Lakes Adventure Gear", [5000, 5000, 7000, 9100]),
                               (2, "Blue Ridge Outfitters", [12000, 15000, 18000]),
                               (3, "Sierra Summit Supply", [40000, 41000])]:
        for a in amounts:
            out.append(OpenInvoice(n, f"INV/2026/{n:05d}", pid, name, None, a))
            n += 1
    out += [OpenInvoice(n + i, f"INV/2026/{n + i:05d}", 9, "Lone Star Basecamp", None,
                        20_000 + i * 731) for i in range(12)]
    return out


def test_every_holdout_case_has_a_written_expectation() -> None:
    for s in build_holdout_plan(invoices()):
        assert s.case in EXPECTED


def test_holdout_covers_the_new_patterns() -> None:
    cases = Counter(s.case for s in build_holdout_plan(invoices()))
    for c in (Case.TRANSFER_SAME_AMOUNT, Case.TRANSFER_TWO_NO_REF, Case.TRANSFER_WRONG_REF,
              Case.TRANSFER_TRANSPOSED, Case.TRANSFER_ABBREV_PAYER, Case.TRANSFER_PO_REF,
              Case.TRANSFER_EARLY, Case.TRANSFER_THIRD_PARTY, Case.CARD_BILLING_NAME_ONLY):
        assert cases[c] >= 1, c


def test_no_invoice_is_used_twice() -> None:
    names = [n for s in build_holdout_plan(invoices()) for n in s.invoice_names]
    assert len(names) == len(set(names))


def test_wrong_ref_cites_an_invoice_that_is_not_the_one_paid() -> None:
    [s] = [s for s in build_holdout_plan(invoices()) if s.case is Case.TRANSFER_WRONG_REF]
    assert s.memo != s.invoices[0].name and s.memo is not None and s.memo.startswith("INV/")


def test_transpose_and_abbreviate() -> None:
    assert transpose("INV/2026/00132") == "INV/2026/00123"
    assert abbreviate("Great Lakes Adventure Gear") == "GT LAKES ADV GR"
