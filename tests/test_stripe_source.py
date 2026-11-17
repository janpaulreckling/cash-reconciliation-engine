"""Stripe extractor tests on responses recorded from the real test-mode API
(tests/fixtures/stripe/*.json), so field names and shapes are Stripe's, not ours."""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from recon.errors import PermanentError
from recon.sources.stripe_source import StripeClientAPI, StripeSource, normalize_charge
from recon.warehouse import Warehouse

FIXTURES = Path(__file__).parent / "fixtures" / "stripe"


def fixture(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text())
    return data


class FakeStripe:
    def __init__(self, objects: dict[str, list[dict[str, Any]]]) -> None:
        self.objects = objects
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def list(self, resource: str, params: dict[str, Any]) -> Iterator[dict[str, Any]]:
        self.calls.append((resource, params))
        items = self.objects.get(resource, [])
        if resource == "balance_transactions" and "payout" in params:
            items = [b for b in items if b.get("_payout") == params["payout"]]
        yield from items


def test_charge_keeps_invoice_reference_from_metadata() -> None:
    row = normalize_charge(fixture("charge"))
    assert row["metadata"]["erp_invoice_number"] == "PROBE/0001"
    assert row["amount"] == 123456 and row["currency"] == "USD"
    assert row["balance_transaction"].startswith("txn_")
    assert row["created"].tzinfo is UTC


def test_fetch_links_charge_and_fee() -> None:
    api = FakeStripe({"charges": [fixture("charge")],
                      "balance_transactions": [fixture("balance_transaction")]})
    data = StripeSource(api).fetch()
    [bt] = data["stripe_balance_transactions"]
    [ch] = data["stripe_charges"]
    assert bt["source"] == ch["id"]
    assert bt["amount"] - bt["fee"] == bt["net"]
    assert bt["payout"] is None  # not swept into a payout yet


def test_balance_transactions_get_their_payout_id() -> None:
    bt = fixture("balance_transaction")
    # Hand-built until test-mode payouts are enabled; replace with a recorded payout then.
    payout = {"id": "po_test", "object": "payout", "amount": bt["net"], "currency": "usd",
              "created": bt["created"], "arrival_date": bt["created"], "status": "paid",
              "method": "standard", "automatic": True, "statement_descriptor": None,
              "trace_id": {"status": "supported", "value": "TRACE123"},
              "balance_transaction": "txn_payout", "failure_code": None}
    api = FakeStripe({"payouts": [payout], "balance_transactions": [{**bt, "_payout": "po_test"}]})
    data = StripeSource(api).fetch()
    assert data["stripe_balance_transactions"][0]["payout"] == "po_test"
    assert data["stripe_payouts"][0]["trace_id"] == "TRACE123"


def test_refund_after_extract_is_stored_as_new_version(warehouse: Warehouse) -> None:
    charge = fixture("charge")
    api = FakeStripe({"charges": [charge]})
    assert warehouse.load("stripe", StripeSource(api).fetch()).total == 1
    assert warehouse.load("stripe", StripeSource(api).fetch()).total == 0  # unchanged

    refunded = copy.deepcopy(charge)
    refunded.update(amount_refunded=charge["amount"], refunded=True)
    api.objects["charges"] = [refunded]
    assert warehouse.load("stripe", StripeSource(api).fetch()).rows_loaded["stripe_charges"] == 1
    versions = warehouse.con.execute(
        "SELECT refunded FROM raw.stripe_charges ORDER BY _loaded_at").fetchall()
    assert versions == [(False,), (True,)]


def test_metadata_is_queryable_json(warehouse: Warehouse) -> None:
    warehouse.load("stripe", StripeSource(FakeStripe({"charges": [fixture("charge")]})).fetch())
    ref = warehouse.con.execute(
        "SELECT metadata->>'erp_invoice_number' FROM raw.stripe_charges").fetchone()
    assert ref == ("PROBE/0001",)


def test_live_keys_are_refused() -> None:
    with pytest.raises(PermanentError, match="live"):
        StripeClientAPI("sk_live_" + "x" * 20)


def test_created_timestamps_are_utc() -> None:
    row = normalize_charge(fixture("charge"))
    assert row["created"] == datetime.fromtimestamp(fixture("charge")["created"], UTC)


def test_manual_payouts_are_not_asked_for_components() -> None:
    """Stripe rejects ?payout= for manual payouts; asking would abort the extract."""
    payout = {"id": "po_manual", "amount": 100, "currency": "usd", "created": 1,
              "arrival_date": 1, "status": "paid", "automatic": False}
    api = FakeStripe({"payouts": [payout]})
    data = StripeSource(api).fetch()
    assert ("balance_transactions", {"payout": "po_manual"}) not in api.calls
    assert data["stripe_payouts"][0]["automatic"] is False
