"""Create the card side of the scenario plan in Stripe test mode, on the test merchant.

Idempotent in two ways: every API call carries an idempotency key derived from the
case id, and a local ledger (`seeds/stripe_ledger.json`) records what was created,
so re-running the seed after a crash finishes the remaining cases without paying
anything twice. Ground truth never goes into Stripe: no case labels in metadata,
because the matching engine reads that metadata.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import stripe
from stripe.params._refund_create_params import RefundCreateParams

from recon.seed.plan import Case, Channel, OpenInvoice, Scenario, card_fee_cents

TOKENS = {Case.CARD_DISPUTED: "tok_createDispute"}  # everything else settles immediately
DEFAULT_TOKEN = "tok_bypassPending"


def customer_email(partner_name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", partner_name.lower()).strip("-")
    return f"ap@{slug}.test"


class StripeSeeder:
    def __init__(self, api_key: str, account: str, ledger_path: Path) -> None:
        if not api_key.startswith("sk_test_"):
            raise ValueError("Test keys only")
        self._client = stripe.StripeClient(api_key, max_network_retries=3)
        self._account = account
        self._ledger_path = ledger_path
        self.ledger: dict[str, Any] = (
            json.loads(ledger_path.read_text()) if ledger_path.exists()
            else {"customers": {}, "cases": {}}
        )

    def _save(self) -> None:
        self._ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self._ledger_path.write_text(json.dumps(self.ledger, indent=2, sort_keys=True))

    def _key(self, *parts: str) -> stripe.RequestOptions:
        return {"stripe_account": self._account,
                "idempotency_key": "recon-seed-v1-" + "-".join(parts)}

    def _customer(self, inv: OpenInvoice) -> str:
        pid = str(inv.partner_id)
        if pid not in self.ledger["customers"]:
            cus = self._client.v1.customers.create(
                {"name": inv.partner_name, "email": customer_email(inv.partner_name),
                 "metadata": {"erp_partner_id": pid}},
                self._key("cus", pid))
            self.ledger["customers"][pid] = cus.id
            self._save()
        return str(self.ledger["customers"][pid])

    def _charge(self, s: Scenario, attempt: str) -> str:
        inv = s.invoices[0]
        customer = self._customer(inv)
        pm = self._client.v1.payment_methods.create(
            {"type": "card", "card": {"token": TOKENS.get(s.case, DEFAULT_TOKEN)},
             "billing_details": {"name": inv.partner_name,
                                 "email": customer_email(inv.partner_name)}},
            self._key(s.case_id, attempt, "pm"))
        self._client.v1.payment_methods.attach(pm.id, {"customer": customer},
                                               self._key(s.case_id, attempt, "attach"))
        metadata: dict[str, str] = {}
        if s.case not in (Case.CARD_NO_METADATA, Case.CARD_BILLING_NAME_ONLY):
            metadata = {"erp_invoice_number": inv.name}
            if inv.opportunity_id:
                metadata["sf_opportunity_id"] = inv.opportunity_id
        pi = self._client.v1.payment_intents.create(
            {"amount": s.pay_cents, "currency": "usd", "customer": customer,
             "payment_method": pm.id, "confirm": True,
             "automatic_payment_methods": {"enabled": True, "allow_redirects": "never"},
             "application_fee_amount": card_fee_cents(s.pay_cents),
             "description": s.memo or "", "metadata": metadata},
            self._key(s.case_id, attempt, "pi"))
        return str(pi.id)

    def run(self, plan: list[Scenario]) -> dict[str, int]:
        done = skipped = 0
        for s in plan:
            if s.channel is not Channel.CARD:
                continue
            if s.case_id in self.ledger["cases"]:
                skipped += 1
                continue
            record: dict[str, Any] = {"case": s.case.value, "invoices": s.invoice_names,
                                      "payment_intents": [], "refunds": []}
            record["payment_intents"].append(self._charge(s, "a"))
            if s.case is Case.CARD_DUPLICATE:
                record["payment_intents"].append(self._charge(s, "b"))
            if s.case in (Case.CARD_REFUNDED, Case.CARD_PARTIAL_REFUND):
                params: RefundCreateParams = {
                    "payment_intent": record["payment_intents"][0],
                    "reason": "requested_by_customer"}
                if "refund_cents" in s.extra:
                    params["amount"] = s.extra["refund_cents"]
                refund = self._client.v1.refunds.create(params, self._key(s.case_id, "refund"))
                record["refunds"].append(refund.id)
            self.ledger["cases"][s.case_id] = record
            self._save()
            done += 1
        return {"created": done, "already_done": skipped}
