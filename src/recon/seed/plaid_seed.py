"""Create the bank side of the scenario plan in the Plaid Sandbox.

One sandbox Item on Plaid's `user_transactions_dynamic` test user plays Northwind's
operating account. `/sandbox/transactions/create` posts the customer transfers from
the plan, Stripe payout deposits (once Stripe has paid out), and lines that have
nothing to do with receivables (payroll, rent, a bank fee, an unidentified deposit).

Plaid sign convention: positive amount = money out, negative = money in.
The endpoint is rate-limited hard, so lines go out in batches of 5 (a batch of 10
was accepted but silently dropped) and the client backs off on
SANDBOX_TRANSACTIONS_CREATE_LIMIT. A ledger
(`seeds/plaid_ledger.json`) makes re-runs post only what is missing.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from recon.seed.plan import Channel, Scenario
from recon.sources.plaid_source import PlaidAPI

SANDBOX_INSTITUTION = "ins_109508"  # First Platypus Bank
BATCH = 5  # 10 per call was accepted (200 OK) but silently dropped in the sandbox


@dataclass(frozen=True)
class BankLine:
    key: str             # ledger key, stable across runs
    day: date
    amount_cents: int    # Plaid convention: negative = money in
    description: str


def transfer_lines(plan: list[Scenario], period_start: date, last_day: date,
                   seed: int = 2026) -> list[BankLine]:
    """Customer payments by ACH/wire, spread over the first half of the period."""
    rng = random.Random(seed + 1)
    span = max(0, (last_day - period_start).days)
    lines = []
    for s in plan:
        if s.channel is not Channel.TRANSFER:
            continue
        payer = s.invoices[0].partner_name.upper()
        kind = "WIRE IN" if s.pay_cents >= 1_000_000 or len(s.invoices) > 1 else "ACH CREDIT"
        memo = (s.memo or "").strip()
        if memo.upper().startswith(payer):  # don't repeat the originator name
            memo = memo[len(payer):].strip()
        description = f"{kind} {payer} {memo}".strip()
        day = period_start + timedelta(days=rng.randint(0, span))
        lines.append(BankLine(f"transfer:{s.case_id}", day, -s.pay_cents, description))
    return lines


def noise_lines(period_start: date) -> list[BankLine]:
    """Bank activity that is not customer cash. The unidentified deposit must end up as
    UNAPPLIED_CASH, the rest must be classified as `other` and ignored."""
    d = period_start
    return [
        BankLine("noise:payroll", d + timedelta(days=0), 1_845_000, "GUSTO PAYROLL NET PAY"),
        BankLine("noise:rent", d + timedelta(days=1), 620_000, "DENVER PROPERTY MGMT RENT NOV"),
        BankLine("noise:fee", d + timedelta(days=13), 2_500, "MONTHLY ACCOUNT SERVICE FEE"),
        BankLine("noise:unidentified", d + timedelta(days=9), -125_000,
                 "DEPOSIT REF 88213 MOBILE CHECK"),
    ]


def payout_lines(payouts: list[tuple[str, int, date, str | None]],
                 seed: int = 2026) -> list[BankLine]:
    """One deposit per Stripe payout (id, amount_cents, arrival_date, trace_id). Most land
    on the arrival date; a few a business day later, as banks do."""
    rng = random.Random(seed + 2)
    lines = []
    for payout_id, amount, arrival, trace in sorted(payouts, key=lambda p: p[2]):
        day = arrival + timedelta(days=1 if rng.random() < 0.3 else 0)
        ref = f" {trace}" if trace else ""
        lines.append(BankLine(f"payout:{payout_id}", day, -amount,
                              f"STRIPE TRANSFER NORTHWIND OUTFITTERS{ref}"))
    return lines


class PlaidSeeder:
    def __init__(self, api: PlaidAPI, ledger_path: Path) -> None:
        self._api = api
        self._ledger_path = ledger_path
        self.ledger: dict[str, Any] = (
            json.loads(ledger_path.read_text()) if ledger_path.exists() else {"posted": {}}
        )

    def _save(self) -> None:
        self._ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self._ledger_path.write_text(json.dumps(self.ledger, indent=2, sort_keys=True))

    def create_item(self) -> str:
        """Create the sandbox Item and return its access token (caller stores it)."""
        public = self._api.post("/sandbox/public_token/create", {
            "institution_id": SANDBOX_INSTITUTION, "initial_products": ["transactions"],
            "options": {"override_username": "user_transactions_dynamic",
                        "override_password": "pass_good"}})
        exchanged = self._api.post("/item/public_token/exchange",
                                   {"public_token": public["public_token"]})
        self.ledger["item_id"] = exchanged["item_id"]
        self.ledger["posted"] = {}
        self._save()
        return str(exchanged["access_token"])

    def post(self, access_token: str, lines: list[BankLine]) -> dict[str, int]:
        todo = [ln for ln in lines if ln.key not in self.ledger["posted"]]
        for i in range(0, len(todo), BATCH):
            chunk = todo[i:i + BATCH]
            self._api.post("/sandbox/transactions/create", {
                "access_token": access_token,
                "transactions": [{
                    "amount": ln.amount_cents / 100, "iso_currency_code": "USD",
                    "date_transacted": ln.day.isoformat(), "date_posted": ln.day.isoformat(),
                    "description": ln.description} for ln in chunk]})
            for ln in chunk:
                self.ledger["posted"][ln.key] = ln.day.isoformat()
            self._save()
        return {"posted": len(todo), "already_posted": len(lines) - len(todo)}
