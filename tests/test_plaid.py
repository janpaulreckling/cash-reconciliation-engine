"""Plaid extractor and seed tests. The transaction shape comes from a recorded real
/transactions/sync response (tests/fixtures/plaid)."""

from __future__ import annotations

import copy
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from recon.errors import PermanentError
from recon.seed.plaid_seed import BATCH, PlaidSeeder, noise_lines, payout_lines, transfer_lines
from recon.seed.plan import Case, Channel, OpenInvoice, Scenario
from recon.sources.plaid_source import PlaidSource, normalize_transaction
from recon.warehouse import Warehouse

FIXTURE = Path(__file__).parent / "fixtures/plaid/transactions_sync_page.json"
PAGE = json.loads(FIXTURE.read_text())
INCOMING = PAGE["added"][0]


class FakePlaid:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self.pages = pages
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((path, body))
        page = self.pages.pop(0)
        if isinstance(page, Exception):
            raise page
        return page


def page(added: list[dict[str, Any]], cursor: str, has_more: bool = False,
         removed: list[str] | None = None) -> dict[str, Any]:
    gone = [{"transaction_id": r} for r in removed or []]
    return {"added": added, "modified": [], "removed": gone, "has_more": has_more,
            "next_cursor": cursor}


def test_incoming_money_is_negative_in_plaid_and_kept_as_is_in_raw() -> None:
    row = normalize_transaction(INCOMING)
    assert row["amount"] == Decimal("-4596.32")  # staging flips the sign, raw stays faithful
    assert row["category"] == "INCOME"
    assert row["removed"] is False


def test_sync_follows_has_more_and_returns_last_cursor() -> None:
    second = copy.deepcopy(INCOMING) | {"transaction_id": "t2"}
    api = FakePlaid([page([INCOMING], "c1", has_more=True), page([second], "c2")])
    rows, removed, cursor = PlaidSource(api, "tok").fetch(None)
    assert [r["id"] for r in rows] == [INCOMING["transaction_id"], "t2"]
    assert cursor == "c2" and removed == []
    assert api.calls[1][1]["cursor"] == "c1"


def test_mutation_during_pagination_restarts_from_original_cursor() -> None:
    api = FakePlaid([
        page([INCOMING], "c1", has_more=True),
        PermanentError("Plaid /transactions/sync: TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION"),
        page([INCOMING], "c9"),
    ])
    rows, _, cursor = PlaidSource(api, "tok").fetch("c0")
    assert cursor == "c9" and len(rows) == 1
    assert api.calls[2][1]["cursor"] == "c0"


def test_cursor_is_saved_with_the_rows(warehouse: Warehouse) -> None:
    rows, _, cursor = PlaidSource(FakePlaid([page([INCOMING], "c1")]), "tok").fetch(None)
    warehouse.load("plaid", {"bank_transactions": rows}, state={"cursor": cursor})
    assert warehouse.state("plaid", "cursor") == "c1"


def test_failed_load_does_not_advance_the_cursor(warehouse: Warehouse) -> None:
    broken = normalize_transaction(INCOMING) | {"date": None}  # violates NOT NULL
    with pytest.raises(Exception):  # noqa: B017 - any DB error must roll back
        warehouse.load("plaid", {"bank_transactions": [broken]}, state={"cursor": "c1"})
    assert warehouse.state("plaid", "cursor") is None


def test_removed_transaction_becomes_a_tombstone_version(warehouse: Warehouse) -> None:
    warehouse.load("plaid", {"bank_transactions": [normalize_transaction(INCOMING)]})
    tomb = warehouse.tombstones("bank_transactions", [INCOMING["transaction_id"], "unknown"])
    warehouse.load("plaid", {"bank_transactions": tomb})
    versions = warehouse.con.execute(
        "SELECT removed FROM raw.bank_transactions ORDER BY _loaded_at").fetchall()
    assert versions == [(False,), (True,)]


def _scenario(case: Case, memo: str | None, pay: int, n: int = 1) -> Scenario:
    invs = [OpenInvoice(i, f"INV/2026/{i:05d}", 7, "Blue Ridge Outfitters", None, pay)
            for i in range(1, n + 1)]
    return Scenario(f"S{case.value}", case, Channel.TRANSFER, invs, pay * n, memo)


def test_transfer_lines_are_inflows_with_bank_style_memos() -> None:
    plan = [_scenario(Case.TRANSFER_EXACT, "INV/2026/00001", 50_000),
            _scenario(Case.TRANSFER_NO_REF, "BLUE RIDGE OUTFITTERS", 50_000),
            _scenario(Case.TRANSFER_BATCH, "Blue Ridge Outfitters payment INV/2026/00001",
                      600_000, 3)]
    lines = transfer_lines(plan, date(2026, 11, 2), date(2026, 11, 16))
    assert all(ln.amount_cents < 0 for ln in lines)
    assert lines[0].description == "ACH CREDIT BLUE RIDGE OUTFITTERS INV/2026/00001"
    assert lines[1].description == "ACH CREDIT BLUE RIDGE OUTFITTERS"  # name not repeated
    assert lines[2].description.startswith("WIRE IN BLUE RIDGE OUTFITTERS payment")
    assert all(date(2026, 11, 2) <= ln.day <= date(2026, 11, 16) for ln in lines)


def test_noise_has_exactly_one_unidentified_deposit() -> None:
    assert [ln.key for ln in noise_lines(date(2026, 11, 2)) if ln.amount_cents < 0] == [
        "noise:unidentified"]


def test_payout_deposits_carry_the_trace_id() -> None:
    [line] = payout_lines([("po_1", 12_345, date(2026, 11, 19), "TRACE9")])
    assert line.amount_cents == -12_345 and line.description.endswith("TRACE9")
    assert line.day in (date(2026, 11, 19), date(2026, 11, 20))


def test_seeder_posts_in_small_batches_and_only_once(tmp_path: Path) -> None:
    lines = noise_lines(date(2026, 11, 2)) * 1 + transfer_lines(
        [_scenario(Case.TRANSFER_EXACT, "INV/2026/00001", 50_000)], date(2026, 11, 2),
        date(2026, 11, 3))
    api = FakePlaid([{"request_id": "r"}] * 10)
    seeder = PlaidSeeder(api, tmp_path / "ledger.json")
    assert seeder.post("tok", lines) == {"posted": 5, "already_posted": 0}
    assert all(len(body["transactions"]) <= BATCH for _, body in api.calls)
    assert PlaidSeeder(api, tmp_path / "ledger.json").post("tok", lines)["posted"] == 0
