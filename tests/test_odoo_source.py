from datetime import UTC, date, datetime
from decimal import Decimal

from recon.sources.odoo import OdooInvoiceSource, normalize_invoice
from tests.conftest import FakeOdoo, odoo_invoice


def test_normalize_flattens_many2one_and_maps_false_to_none() -> None:
    row = normalize_invoice(odoo_invoice(1, "2026-11-17 10:00:00", ref=False,
                                         invoice_origin=False))
    assert row["partner_id"] == 7
    assert row["partner_name"] == "Rocky Mountain Trail Supply"
    assert row["currency"] == "USD"
    assert row["ref"] is None and row["invoice_origin"] is None
    assert row["invoice_date_due"] == date(2026, 12, 17)
    assert row["write_date"] == datetime(2026, 11, 17, 10, 0, tzinfo=UTC)
    assert row["reversed_entry_id"] is None


def test_credit_note_keeps_link_to_reversed_invoice() -> None:
    row = normalize_invoice(odoo_invoice(9, "2026-11-17 10:00:00", move_type="out_refund",
                                         reversed_entry_id=[72, "INV/2026/00072"]))
    assert row["reversed_entry_id"] == 72


def test_money_is_decimal_rounded_to_cents_not_float() -> None:
    row = normalize_invoice(odoo_invoice(1, "2026-11-17 10:00:00", amount_total=0.1 + 0.2))
    assert row["amount_total"] == Decimal("0.30")


def test_all_states_of_customer_moves_are_read_vendor_bills_are_not() -> None:
    fake = FakeOdoo([
        odoo_invoice(1, "2026-11-17 10:00:00"),
        odoo_invoice(2, "2026-11-17 10:00:00", state="draft"),
        odoo_invoice(3, "2026-11-17 10:00:00", move_type="in_invoice"),
        odoo_invoice(4, "2026-11-17 10:00:00", move_type="out_refund"),
    ])
    assert [r["id"] for r in OdooInvoiceSource(fake).fetch()] == [1, 2, 4]


def test_incremental_pull_includes_rows_at_the_watermark_second() -> None:
    fake = FakeOdoo([
        odoo_invoice(1, "2026-11-17 09:59:59"),
        odoo_invoice(2, "2026-11-17 10:00:00"),
        odoo_invoice(3, "2026-11-17 10:00:01"),
    ])
    since = datetime(2026, 11, 17, 10, 0, tzinfo=UTC)
    assert [r["id"] for r in OdooInvoiceSource(fake).fetch(since)] == [2, 3]


def test_keyset_paging_reads_every_row_once() -> None:
    fake = FakeOdoo([odoo_invoice(i, "2026-11-17 10:00:00") for i in range(1, 6)])
    ids = [r["id"] for r in OdooInvoiceSource(fake, page_size=2).fetch()]
    assert ids == [1, 2, 3, 4, 5]
    assert all(c["offset"] == 0 for c in fake.calls)


def test_invoice_paid_during_extract_does_not_make_another_row_vanish() -> None:
    rows = [odoo_invoice(i, f"2026-11-17 10:00:0{i}") for i in range(1, 6)]
    fake = FakeOdoo(rows)

    def pay_invoice_1(calls: int) -> None:
        if calls == 1:  # after the first page was served, invoice 1 gets paid
            rows[0] = odoo_invoice(1, "2026-11-17 11:00:00", payment_state="paid")

    fake.on_page = pay_invoice_1
    seen = [(r["id"], r["payment_state"]) for r in OdooInvoiceSource(fake, page_size=2).fetch()]
    assert {i for i, _ in seen} == {1, 2, 3, 4, 5}
    assert (1, "paid") in seen  # the new version is picked up in the same run
