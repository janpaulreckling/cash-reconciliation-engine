"""Read-only extractor for customer invoices from Odoo 17 (XML-RPC external API).

This repo never writes to the ERP. Applying cash to invoices stays a human decision;
the engine only proposes journal entries.

Identity chain (verified against live Odoo 17, see docs/data_model.md section 2):
    account.move.name               invoice number, e.g. INV/2026/00012
    account.move.payment_reference  what the customer should quote on a transfer (= name)
    account.move.invoice_origin     sale order name(s), comma-separated on combined invoices
    account.move.ref                copied from sale.order.client_order_ref
                                    (= Salesforce Opportunity Id) when the invoice is created
"""

from __future__ import annotations

import socket
import xmlrpc.client
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

from recon.errors import PermanentError, TransientError

ODOO_DT = "%Y-%m-%d %H:%M:%S"
CUSTOMER_MOVE_TYPES = ("out_invoice", "out_refund")
INVOICE_FIELDS = [
    "id", "name", "move_type", "state", "partner_id", "invoice_origin", "ref",
    "payment_reference", "invoice_date", "invoice_date_due", "currency_id",
    "amount_total", "amount_residual", "payment_state", "reversed_entry_id", "write_date",
]
CENT = Decimal("0.01")


class OdooRPC(Protocol):
    """The one call we need from Odoo; lets tests swap in a fake."""

    def search_read(
        self, model: str, domain: list[Any], fields: list[str], *,
        order: str, limit: int, offset: int,
    ) -> list[dict[str, Any]]: ...


class XmlRpcOdoo:
    """Thin XML-RPC client with error classification and retry on transient failures."""

    def __init__(self, url: str, db: str, username: str, password: str) -> None:
        self._db, self._username, self._password = db, username, password
        self._common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common", allow_none=True)
        self._models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object", allow_none=True)
        self._uid: int | None = None

    @retry(
        retry=retry_if_exception_type(TransientError),
        stop=stop_after_attempt(5),
        wait=wait_random_exponential(multiplier=0.5, max=10),
        reraise=True,
    )
    def search_read(
        self, model: str, domain: list[Any], fields: list[str], *,
        order: str, limit: int, offset: int,
    ) -> list[dict[str, Any]]:
        try:
            if self._uid is None:
                uid: Any = self._common.authenticate(self._db, self._username, self._password, {})
                if not uid:
                    raise PermanentError("Odoo authentication failed; check credentials")
                self._uid = int(uid)
            rows: Any = self._models.execute_kw(
                self._db, self._uid, self._password, model, "search_read", [domain],
                {"fields": fields, "order": order, "limit": limit, "offset": offset},
            )
            return list(rows)
        except (ConnectionError, TimeoutError, socket.gaierror) as exc:
            raise TransientError(f"Odoo unreachable: {exc}") from exc
        except xmlrpc.client.ProtocolError as exc:
            if exc.errcode >= 500 or exc.errcode == 429:
                raise TransientError(f"Odoo HTTP {exc.errcode}") from exc
            raise PermanentError(f"Odoo HTTP {exc.errcode}: {exc.errmsg}") from exc
        except xmlrpc.client.Fault as exc:
            last = exc.faultString.strip().splitlines()[-1]
            raise PermanentError(f"Odoo {model}.search_read: {last}") from exc


def parse_odoo_dt(value: str) -> datetime:
    return datetime.strptime(value, ODOO_DT).replace(tzinfo=UTC)


def format_odoo_dt(value: datetime) -> str:
    return value.astimezone(UTC).strftime(ODOO_DT)


def _text(value: Any) -> str | None:
    # Odoo serializes empty char fields as False over XML-RPC.
    return value if isinstance(value, str) and value else None


def _money(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(CENT)


def normalize_invoice(row: dict[str, Any]) -> dict[str, Any]:
    """Flatten many2one pairs and replace Odoo's False-for-empty with None."""
    partner = row.get("partner_id") or [None, None]
    currency = row.get("currency_id") or [None, None]
    invoice_date = _text(row.get("invoice_date"))
    due_date = _text(row.get("invoice_date_due"))
    return {
        "id": int(row["id"]),
        "name": row["name"],
        "move_type": row["move_type"],
        "state": row["state"],
        "partner_id": partner[0],
        "partner_name": partner[1],
        "invoice_origin": _text(row.get("invoice_origin")),
        "ref": _text(row.get("ref")),
        "payment_reference": _text(row.get("payment_reference")),
        "invoice_date": datetime.strptime(invoice_date, "%Y-%m-%d").date()
        if invoice_date else None,
        "invoice_date_due": datetime.strptime(due_date, "%Y-%m-%d").date() if due_date else None,
        "currency": currency[1],
        "amount_total": _money(row["amount_total"]),
        "amount_residual": _money(row["amount_residual"]),
        "payment_state": row["payment_state"],
        # credit note -> the invoice it reverses (Odoo then sets payment_state=reversed)
        "reversed_entry_id": (row.get("reversed_entry_id") or [None])[0],
        "write_date": parse_odoo_dt(row["write_date"]),
    }


class OdooInvoiceSource:
    """Yields customer invoices and credit notes changed since a watermark.

    Payments change `amount_residual`/`payment_state` on the invoice and bump its
    `write_date`, so an incremental pull by `write_date` also catches status changes.

    Two deliberate choices:
    - All states are extracted, not just `posted`. A posted invoice that is later
      reset to draft or cancelled must reach raw as a new version, or it would stay
      open AR forever. Staging filters to the current posted version.
    - Keyset paging on (write_date, id) instead of offsets. If an invoice is paid
      while the extract runs, it moves to the end of the ordering; with offsets every
      later row would shift and one would be skipped for good.
    The first page uses `>=` so rows sharing the watermark second are re-read; the
    warehouse skips versions it already stores.
    """

    model = "account.move"

    def __init__(self, rpc: OdooRPC, page_size: int = 500) -> None:
        self._rpc = rpc
        self._page_size = page_size

    def fetch(self, since: datetime | None = None) -> Iterator[dict[str, Any]]:
        base: list[Any] = [("move_type", "in", list(CUSTOMER_MOVE_TYPES))]
        cursor: list[Any] = [] if since is None else [("write_date", ">=", format_odoo_dt(since))]
        while True:
            page = self._rpc.search_read(
                self.model, base + cursor, INVOICE_FIELDS,
                order="write_date asc, id asc", limit=self._page_size, offset=0,
            )
            for row in page:
                yield normalize_invoice(row)
            if len(page) < self._page_size:
                return
            last_dt, last_id = page[-1]["write_date"], page[-1]["id"]
            cursor = ["|", ("write_date", ">", last_dt),
                      "&", ("write_date", "=", last_dt), ("id", ">", last_id)]
