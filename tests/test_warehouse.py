from datetime import UTC, datetime

import pytest

from recon.errors import TransientError
from recon.sources.odoo import OdooInvoiceSource
from recon.warehouse import Warehouse
from tests.conftest import FakeOdoo, odoo_invoice


def _extract(wh: Warehouse, fake: FakeOdoo) -> int:
    since = wh.watermark("odoo_invoices")
    rows = OdooInvoiceSource(fake, page_size=2).fetch(since)
    result = wh.load("odoo_invoices", {"erp_invoices": rows}, since=since,
                     cursor=("erp_invoices", "write_date"))
    return result.total


def test_first_run_loads_everything_and_sets_watermark(warehouse: Warehouse) -> None:
    fake = FakeOdoo([
        odoo_invoice(1, "2026-11-17 10:00:00"),
        odoo_invoice(2, "2026-11-17 11:00:00"),
    ])
    assert _extract(warehouse, fake) == 2
    assert warehouse.watermark("odoo_invoices") == datetime(2026, 11, 17, 11, 0, tzinfo=UTC)


def test_payment_on_invoice_is_picked_up_as_new_version(warehouse: Warehouse) -> None:
    fake = FakeOdoo([odoo_invoice(1, "2026-11-17 10:00:00")])
    _extract(warehouse, fake)
    fake.rows[0] = odoo_invoice(1, "2026-11-18 09:00:00", amount_residual=0.0,
                                payment_state="paid")
    _extract(warehouse, fake)
    versions = warehouse.con.execute(
        "SELECT payment_state FROM raw.erp_invoices WHERE id = 1 ORDER BY write_date"
    ).fetchall()
    assert versions == [("not_paid",), ("paid",)]


def test_rerun_without_changes_stores_nothing_new(warehouse: Warehouse) -> None:
    fake = FakeOdoo([odoo_invoice(i, "2026-11-17 10:00:00") for i in (1, 2)])
    _extract(warehouse, fake)
    assert _extract(warehouse, fake) == 0  # boundary rows re-read, but already stored
    assert warehouse.row_counts()["erp_invoices"] == 2
    assert warehouse.watermark("odoo_invoices") == datetime(2026, 11, 17, 10, 0, tzinfo=UTC)


def test_new_row_in_the_watermark_second_is_not_lost(warehouse: Warehouse) -> None:
    fake = FakeOdoo([odoo_invoice(1, "2026-11-17 10:00:00")])
    _extract(warehouse, fake)
    fake.rows.append(odoo_invoice(2, "2026-11-17 10:00:00"))  # committed late, same second
    assert _extract(warehouse, fake) == 1


def test_failed_run_loads_nothing_and_keeps_watermark(warehouse: Warehouse) -> None:
    fake = FakeOdoo([odoo_invoice(i, f"2026-11-17 10:00:{i:02d}") for i in range(1, 6)])
    fake.fail_after_calls = 1  # second page blows up
    with pytest.raises(TransientError):
        _extract(warehouse, fake)
    assert warehouse.row_counts()["erp_invoices"] == 0
    assert warehouse.watermark("odoo_invoices") is None
    [(source, _, status, _, _, error)] = warehouse.recent_runs()
    assert (source, status) == ("odoo_invoices", "failed")
    assert "TransientError" in error


def test_posted_invoice_cancelled_later_reaches_raw(warehouse: Warehouse) -> None:
    fake = FakeOdoo([odoo_invoice(1, "2026-11-17 10:00:00")])
    _extract(warehouse, fake)
    fake.rows[0] = odoo_invoice(1, "2026-11-18 09:00:00", state="cancel")
    _extract(warehouse, fake)
    latest = warehouse.con.execute(
        "SELECT state FROM raw.erp_invoices WHERE id = 1 ORDER BY write_date DESC LIMIT 1"
    ).fetchone()
    assert latest == ("cancel",)
