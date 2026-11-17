"""The evaluation must catch the expensive error: right invoice, wrong customer's money."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb

from recon.evaluate import evaluate, metrics
from recon.warehouse import Warehouse


def _setup(tmp_path: Path, decisions: list[tuple[str, str | None, int | None, str | None]]
           ) -> tuple[duckdb.DuckDBPyConnection, Path, Path]:
    wh = Warehouse(str(tmp_path / "t.duckdb"))
    wh.con.execute("INSERT INTO engine.match_runs VALUES ('r1', now(), '{}', 2)")
    for sid, inv, tier, exc in decisions:
        wh.con.execute(
            "INSERT INTO engine.match_decisions VALUES ('r1', 'bank_receipt', ?, ?, 1.0, 'x',"
            " ?, 100, 0, ?, ?, '{}')", [sid, tier, inv, exc, json.dumps([])])
    expected = tmp_path / "expected.csv"
    expected.write_text("case_id,case,channel,invoice,pay_cents,expected_tier,expected_exception\n"
                        "S1,transfer_exact,transfer,INV/2026/00001,100,1,\n"
                        "S2,transfer_exact,transfer,INV/2026/00002,100,1,\n")
    cash = tmp_path / "cash.csv"
    cash.write_text("case_id,source,source_id\nS1,bank_receipt,b1\nS2,bank_receipt,b2\n")
    return wh.con, expected, cash


def test_correct_pairs_score_full_marks(tmp_path: Path) -> None:
    con, exp, cash = _setup(tmp_path, [("b1", "INV/2026/00001", 1, None),
                                       ("b2", "INV/2026/00002", 1, None)])
    m = metrics(evaluate(con, exp, cash))
    assert (m["correct"], m["precision"], m["recall"]) == (2, 1.0, 1.0)


def test_swapped_cash_is_a_precision_miss_even_though_tiers_look_right(tmp_path: Path) -> None:
    con, exp, cash = _setup(tmp_path, [("b1", "INV/2026/00002", 1, None),
                                       ("b2", "INV/2026/00001", 1, None)])
    ev = evaluate(con, exp, cash)
    m = metrics(ev)
    assert m["precision"] == 0.0 and m["recall"] == 0.0 and m["correct"] == 0
    assert all("WRONG_CASH_LINKED" in o.exceptions for o in ev.outcomes)
