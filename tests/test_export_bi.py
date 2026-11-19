"""The BI export: one row per payment, worst status wins, totals tie to the close summary."""

from __future__ import annotations

import csv
import importlib.util
from pathlib import Path
from types import ModuleType

import duckdb
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "export_bi.py"


def load_export() -> ModuleType:
    spec = importlib.util.spec_from_file_location("export_bi", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_db(path: Path, cash_items: int, cash_cents: int) -> None:
    con = duckdb.connect(str(path))
    con.execute("create schema marts")
    con.execute("""
        create table marts.fct_cash_decisions (
            source varchar, source_id varchar, cash_date date, amount_cents bigint,
            partner_name varchar, status varchar, tier integer, exception_code varchar)
    """)
    con.execute("""
        insert into marts.fct_cash_decisions values
        -- one payment, two invoices: one line matched exactly, the other line failed
        ('bank', 'tx-mixed', '2026-11-10', 50000, 'Acme', 'auto_matched', 1, null),
        ('bank', 'tx-mixed', '2026-11-10', 50000, 'Acme', 'exception', null, 'AMOUNT_MISMATCH'),
        ('stripe_charge', 'ch-ok', '2026-11-17', 12000, 'Bolt', 'auto_matched', 2, null),
        -- payouts are transfers, not customer payments
        ('stripe_payout', 'po-1', '2026-11-17', 99999, null, 'auto_matched', 3, null)
    """)
    con.execute("create table marts.rpt_close_status "
                "(matched_at timestamp, cash_items integer, cash_cents bigint)")
    con.execute("insert into marts.rpt_close_status values (now(), ?, ?)", [cash_items, cash_cents])
    con.close()


def test_worst_status_carries_its_own_tier_and_code(tmp_path: Path) -> None:
    make_db(tmp_path / "w.duckdb", cash_items=2, cash_cents=62000)
    assert load_export().export(tmp_path / "w.duckdb", tmp_path / "out") == 2

    rows = {r["source_id"]: r for r in csv.DictReader((tmp_path / "out/cash_items.csv").open())}
    assert set(rows) == {"tx-mixed", "ch-ok"}
    mixed = rows["tx-mixed"]
    assert (mixed["status"], mixed["match_tier"], mixed["exception_code"]) == (
        "Unmatched", "Not matched", "AMOUNT_MISMATCH")
    assert rows["ch-ok"]["match_tier"] == "Tier 2 · Fuzzy"
    assert mixed["aging_bucket"] == "4-7 days"  # 7 days before the latest payment


def test_export_refuses_totals_that_do_not_tie(tmp_path: Path) -> None:
    make_db(tmp_path / "w.duckdb", cash_items=2, cash_cents=61999)
    with pytest.raises(SystemExit, match="does not tie"):
        load_export().export(tmp_path / "w.duckdb", tmp_path / "out")
