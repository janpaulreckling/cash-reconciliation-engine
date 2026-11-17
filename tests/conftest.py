from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from recon.warehouse import Warehouse


def odoo_invoice(id_: int, write_date: str, **overrides: Any) -> dict[str, Any]:
    """An account.move row as Odoo 17 returns it over XML-RPC (shape taken from live Odoo)."""
    row: dict[str, Any] = {
        "id": id_,
        "name": f"INV/2026/{id_:05d}",
        "move_type": "out_invoice",
        "state": "posted",
        "partner_id": [7, "Rocky Mountain Trail Supply"],
        "invoice_origin": f"S{id_:05d}",
        "ref": "006gK00000SXRirQAH",
        "payment_reference": f"INV/2026/{id_:05d}",
        "invoice_date": "2026-11-17",
        "invoice_date_due": "2026-12-17",
        "currency_id": [1, "USD"],
        "amount_total": 8955.0,
        "amount_residual": 8955.0,
        "payment_state": "not_paid",
        "reversed_entry_id": False,
        "write_date": write_date,
    }
    row.update(overrides)
    return row


class FakeOdoo:
    """In-memory stand-in for the Odoo search_read call, honoring the domain we send."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[dict[str, Any]] = []
        self.fail_after_calls: int | None = None
        self.on_page: Callable[[int], None] | None = None  # hook to mutate data mid-extract

    def search_read(
        self, model: str, domain: list[Any], fields: list[str], *,
        order: str, limit: int, offset: int,
    ) -> list[dict[str, Any]]:
        if self.fail_after_calls is not None and len(self.calls) >= self.fail_after_calls:
            from recon.errors import TransientError
            raise TransientError("Odoo unreachable")
        self.calls.append({"model": model, "domain": domain, "limit": limit, "offset": offset})
        matching = [r for r in self.rows if self._eval(r, domain)]
        matching.sort(key=lambda r: (r["write_date"], r["id"]))
        page = [{f: r[f] for f in fields} for r in matching[offset:offset + limit]]
        if self.on_page is not None:
            self.on_page(len(self.calls))
        return page

    @classmethod
    def _eval(cls, row: dict[str, Any], domain: list[Any]) -> bool:
        """Odoo domains: implicit AND, prefix '|' / '&' operators."""
        def parse(i: int) -> tuple[bool, int]:
            tok = domain[i]
            if tok in ("|", "&"):
                a, i = parse(i + 1)
                b, i = parse(i)
                return (a or b) if tok == "|" else (a and b), i
            return cls._match(row, tok), i + 1

        i, result = 0, True
        while i < len(domain):
            ok, i = parse(i)
            result = result and ok
        return result

    @staticmethod
    def _match(row: dict[str, Any], cond: tuple[str, str, Any]) -> bool:
        field, op, value = cond
        ops = {
            "in": lambda a, b: a in b,
            "=": lambda a, b: a == b,
            ">": lambda a, b: a > b,
            ">=": lambda a, b: a >= b,
        }
        return bool(ops[op](row[field], value))


@pytest.fixture
def warehouse() -> Warehouse:
    return Warehouse(":memory:")
