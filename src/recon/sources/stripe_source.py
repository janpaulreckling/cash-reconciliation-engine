"""Extractor for Stripe (test mode): charges, refunds, disputes, balance transactions, payouts.

Why a full pull instead of an incremental one: Stripe objects have no `updated`
timestamp, and the fields reconciliation cares about change after creation (a charge
gets refunded or disputed, a payout goes from `pending` to `paid`, a balance
transaction gets swept into a payout). Test-mode volume is small, so every run reads
everything and the warehouse stores only versions whose content changed. At
production volume this would switch to the Events API or Stripe's payout
reconciliation report.

The payout -> balance transaction link is not a field on the balance transaction;
Stripe exposes it as a filter (`GET /v1/balance_transactions?payout=po_...`), and only
for automatic payouts. We ask once per automatic payout and stamp the payout id onto
each transaction. That's also why the demo account keeps Stripe's automatic daily
payout schedule instead of creating manual payouts.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, Protocol

import stripe

from recon.errors import PermanentError, TransientError

RESOURCES = ("charges", "refunds", "disputes", "balance_transactions", "payouts")


class StripeAPI(Protocol):
    def list(self, resource: str, params: dict[str, Any]) -> Iterator[dict[str, Any]]: ...


class StripeClientAPI:
    """Auto-paginating list calls over the official SDK, with error classification."""

    def __init__(self, api_key: str) -> None:
        if not api_key.startswith(("sk_test_", "rk_test_")):
            raise PermanentError("Refusing to run against a live Stripe key; use sk_test_...")
        self._client = stripe.StripeClient(api_key, max_network_retries=3)

    def list(self, resource: str, params: dict[str, Any]) -> Iterator[dict[str, Any]]:
        service = getattr(self._client.v1, resource)
        try:
            for obj in service.list({"limit": 100, **params}).auto_paging_iter():
                yield obj.to_dict()
        except (stripe.APIConnectionError, stripe.RateLimitError) as exc:
            raise TransientError(f"Stripe {resource}: {exc.user_message or exc}") from exc
        except stripe.StripeError as exc:
            raise PermanentError(f"Stripe {resource}: {exc.user_message or exc}") from exc


def _ts(value: int | None) -> datetime | None:
    return datetime.fromtimestamp(value, UTC) if value is not None else None


def _id(value: Any) -> str | None:
    """Expandable fields arrive as an id string, or as an object if expanded."""
    if value is None or isinstance(value, str):
        return value
    return str(value["id"])


def normalize_charge(c: dict[str, Any]) -> dict[str, Any]:
    billing = c.get("billing_details") or {}
    return {
        "id": c["id"],
        "payment_intent": _id(c.get("payment_intent")),
        "amount": c["amount"],
        "amount_captured": c.get("amount_captured"),
        "amount_refunded": c.get("amount_refunded"),
        "currency": c["currency"].upper(),
        "created": _ts(c["created"]),
        "status": c.get("status"),
        "paid": c.get("paid"),
        "refunded": c.get("refunded"),
        "disputed": c.get("disputed"),
        "customer": _id(c.get("customer")),
        "billing_email": billing.get("email") or c.get("receipt_email"),
        "billing_name": billing.get("name"),
        "description": c.get("description"),
        "metadata": dict(c.get("metadata") or {}),
        "balance_transaction": _id(c.get("balance_transaction")),
    }


def normalize_refund(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": r["id"],
        "charge": _id(r.get("charge")),
        "payment_intent": _id(r.get("payment_intent")),
        "amount": r["amount"],
        "currency": r["currency"].upper(),
        "created": _ts(r["created"]),
        "status": r.get("status"),
        "reason": r.get("reason"),
        "balance_transaction": _id(r.get("balance_transaction")),
    }


def normalize_dispute(d: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": d["id"],
        "charge": _id(d.get("charge")),
        "payment_intent": _id(d.get("payment_intent")),
        "amount": d["amount"],
        "currency": d["currency"].upper(),
        "created": _ts(d["created"]),
        "status": d.get("status"),
        "reason": d.get("reason"),
    }


def normalize_balance_transaction(b: dict[str, Any], payout: str | None) -> dict[str, Any]:
    return {
        "id": b["id"],
        "type": b["type"],
        "reporting_category": b.get("reporting_category"),
        "source": _id(b.get("source")),
        "amount": b["amount"],
        "fee": b["fee"],
        "net": b["net"],
        "currency": b["currency"].upper(),
        "created": _ts(b["created"]),
        "available_on": _ts(b.get("available_on")),
        "status": b.get("status"),
        "description": b.get("description"),
        "payout": payout,
    }


def normalize_payout(p: dict[str, Any]) -> dict[str, Any]:
    trace = p.get("trace_id") or {}
    return {
        "id": p["id"],
        "amount": p["amount"],
        "currency": p["currency"].upper(),
        "created": _ts(p["created"]),
        "arrival_date": _ts(p.get("arrival_date")),
        "status": p.get("status"),
        "method": p.get("method"),
        "automatic": p.get("automatic"),
        "statement_descriptor": p.get("statement_descriptor"),
        "trace_id": trace.get("value") if isinstance(trace, dict) else None,
        "balance_transaction": _id(p.get("balance_transaction")),
        "failure_code": p.get("failure_code"),
    }


class StripeSource:
    def __init__(self, api: StripeAPI) -> None:
        self._api = api

    def fetch(self) -> dict[str, list[dict[str, Any]]]:
        payouts = [normalize_payout(p) for p in self._api.list("payouts", {})]
        payout_of: dict[str, str] = {}
        # Stripe only lists the components of *automatic* payouts; for a manual payout
        # the filter is an invalid request. Manual payouts therefore have no component
        # list, and the A2 sum check flags them as an exception instead.
        for p in (p for p in payouts if p["automatic"]):
            for b in self._api.list("balance_transactions", {"payout": p["id"]}):
                if b["type"] not in ("payout", "payout_cancel", "payout_failure"):
                    payout_of[b["id"]] = p["id"]
        return {
            "stripe_charges": [normalize_charge(c) for c in self._api.list("charges", {})],
            "stripe_refunds": [normalize_refund(r) for r in self._api.list("refunds", {})],
            "stripe_disputes": [normalize_dispute(d) for d in self._api.list("disputes", {})],
            "stripe_balance_transactions": [
                normalize_balance_transaction(b, payout_of.get(b["id"]))
                for b in self._api.list("balance_transactions", {})
            ],
            "stripe_payouts": payouts,
        }
