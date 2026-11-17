"""Bank transactions from Plaid (Sandbox) via /transactions/sync.

Plaid's sync endpoint is cursor-based: each call returns what was added, modified
or removed since the cursor. The warehouse stores the new cursor in the same
transaction as the rows, so a crash can never skip transactions. Removed
transactions are kept as a tombstone version (`removed = true`) rather than
deleted, so the audit trail shows that the bank retracted them.

If Plaid reports a mutation during pagination, the whole sync restarts from the
original cursor, as Plaid's docs require.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import date
from decimal import Decimal
from typing import Any, Protocol

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

from recon.errors import PermanentError, TransientError

SANDBOX = "https://sandbox.plaid.com"
TRANSIENT_CODES = {"RATE_LIMIT_EXCEEDED", "INTERNAL_SERVER_ERROR", "PLANNED_MAINTENANCE",
                   "PRODUCT_NOT_READY", "SANDBOX_TRANSACTIONS_CREATE_LIMIT"}


class PlaidAPI(Protocol):
    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]: ...


class PlaidClient:
    """Minimal JSON client. Credentials go in the body, never in URLs or logs."""

    def __init__(self, client_id: str, secret: str, base_url: str = SANDBOX) -> None:
        if not base_url.startswith(SANDBOX):
            raise PermanentError("Only the Plaid Sandbox is supported")
        self._auth = {"client_id": client_id, "secret": secret}
        self._base = base_url

    @retry(retry=retry_if_exception_type(TransientError), stop=stop_after_attempt(6),
           wait=wait_random_exponential(multiplier=2, max=60), reraise=True)
    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        req = urllib.request.Request(
            self._base + path, json.dumps({**self._auth, **body}).encode(),
            {"Content-Type": "application/json"})
        try:
            result: dict[str, Any] = json.load(urllib.request.urlopen(req, timeout=60))
            return result
        except urllib.error.HTTPError as exc:
            err = json.load(exc)
            code, msg = err.get("error_code", ""), err.get("error_message", "")
            if code in TRANSIENT_CODES or exc.code >= 500:
                raise TransientError(f"Plaid {path}: {code} {msg}") from exc
            raise PermanentError(f"Plaid {path}: {code} {msg}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise TransientError(f"Plaid unreachable: {exc}") from exc


def _date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def normalize_transaction(t: dict[str, Any], removed: bool = False) -> dict[str, Any]:
    pfc = t.get("personal_finance_category") or {}
    return {
        "id": t["transaction_id"],
        "account_id": t["account_id"],
        "date": _date(t["date"]),
        "authorized_date": _date(t.get("authorized_date")),
        "amount": Decimal(str(t["amount"])).quantize(Decimal("0.01")),
        "iso_currency_code": t.get("iso_currency_code"),
        "name": t.get("name"),
        "merchant_name": t.get("merchant_name"),
        "original_description": t.get("original_description"),
        "payment_channel": t.get("payment_channel"),
        "category": pfc.get("primary"),
        "category_detailed": pfc.get("detailed"),
        "pending": t.get("pending"),
        "removed": removed,
    }


def normalize_account(a: dict[str, Any]) -> dict[str, Any]:
    balances = a.get("balances") or {}
    return {"id": a["account_id"], "name": a.get("name"), "mask": a.get("mask"),
            "type": a.get("type"), "subtype": a.get("subtype"),
            "iso_currency_code": balances.get("iso_currency_code")}


class PlaidSource:
    def __init__(self, api: PlaidAPI, access_token: str) -> None:
        self._api = api
        self._token = access_token

    def accounts(self) -> list[dict[str, Any]]:
        page = self._api.post("/accounts/get", {"access_token": self._token})
        return [normalize_account(a) for a in page["accounts"]]

    def fetch(self, cursor: str | None) -> tuple[list[dict[str, Any]], list[str], str]:
        """All changes since `cursor`: (added/modified rows, removed ids, next_cursor)."""
        for _ in range(3):
            rows: list[dict[str, Any]] = []
            removed_ids: list[str] = []
            next_cursor = cursor or ""
            try:
                while True:
                    page = self._api.post("/transactions/sync", {
                        "access_token": self._token, "cursor": next_cursor, "count": 500,
                        "options": {"include_original_description": True}})
                    rows += [normalize_transaction(t) for t in page["added"] + page["modified"]]
                    removed_ids += [r["transaction_id"] for r in page["removed"]]
                    next_cursor = page["next_cursor"]
                    if not page["has_more"]:
                        break
            except PermanentError as exc:
                if "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION" in str(exc):
                    continue  # restart from the original cursor
                raise
            return rows, removed_ids, next_cursor
        raise TransientError("Plaid kept mutating during pagination; retry later")
