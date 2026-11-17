"""Create the Northwind Outfitters test merchant as a Stripe Connect account (test mode).

Why: a Stripe sandbox refuses test bank accounts on the platform account itself, so it
cannot pay out. A connected account can, with Stripe's documented test values
(routing 110000000 / account 000123456789, address_full_match, SSN 000000000, DOB
1901-01-01). Accounts v1 creation is closed for new platforms, so this uses
POST /v2/core/accounts and v1 only for the external bank account.

The platform collects fees (`fees_collector=application`), so the seed charges
Stripe's standard 2.9% + 30c as an application fee; it then shows up as `fee` on the
merchant's balance transactions, which is what reconciliation needs.

Usage: uv run python scripts/setup_stripe_merchant.py   -> prints RECON_STRIPE_ACCOUNT=acct_...
Only test keys are accepted. All identity data is Stripe test data for a fictional company.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from recon.config import Settings

API = "https://api.stripe.com"
V2_VERSION = "2026-10-28.endive"
ADDRESS = {"line1": "address_full_match", "city": "Denver", "state": "CO",
           "postal_code": "80202", "country": "us"}


def call(key: str, method: str, path: str, body: dict[str, Any] | None = None,
         form: dict[str, str] | None = None) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {key}"}
    data = None
    if body is not None:
        headers |= {"Content-Type": "application/json", "Stripe-Version": V2_VERSION}
        data = json.dumps(body).encode()
    elif form is not None:
        data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(API + path, data, headers, method=method)
    try:
        result: dict[str, Any] = json.load(urllib.request.urlopen(req))
    except urllib.error.HTTPError as exc:
        result = json.load(exc)
    if "error" in result:
        raise SystemExit(f"{method} {path}: {result['error'].get('message')}")
    return result


def main() -> None:
    settings = Settings()
    if settings.stripe_api_key is None:
        raise SystemExit("RECON_STRIPE_API_KEY is not set")
    key = settings.stripe_api_key.get_secret_value()
    if not key.startswith("sk_test_"):
        raise SystemExit("Test keys only")

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    acct = call(key, "POST", "/v2/core/accounts", {
        "contact_email": "merchant@northwind-outfitters.test",
        "display_name": "Northwind Outfitters",
        "dashboard": "none",
        "identity": {
            "country": "us", "entity_type": "company",
            "attestations": {"terms_of_service": {"account": {"date": now, "ip": "127.0.0.1"}},
                             "persons_provided": {"owners": True}},
            "business_details": {"registered_name": "Northwind Outfitters", "phone": "0000000000",
                                 "id_numbers": [{"type": "us_ein", "value": "000000000"}],
                                 "address": ADDRESS},
        },
        "configuration": {"merchant": {
            "mcc": "5941", "capabilities": {"card_payments": {"requested": True}},
            "statement_descriptor": {"descriptor": "NORTHWIND OUTFITTERS"}}},
        "defaults": {"currency": "usd",
                     "profile": {"product_description": "Outdoor gear wholesale (fictional)"},
                     "responsibilities": {"fees_collector": "application",
                                          "losses_collector": "application"}},
        "metadata": {"purpose": "recon-demo-merchant"},
    })
    account_id = acct["id"]
    call(key, "POST", f"/v2/core/accounts/{account_id}/persons", {
        "given_name": "Jenny", "surname": "Rosen", "phone": "0000000000",
        "email": "jenny.rosen@northwind-outfitters.test",
        "date_of_birth": {"day": 1, "month": 1, "year": 1901}, "address": ADDRESS,
        "id_numbers": [{"type": "us_ssn", "value": "000000000"}],
        "relationship": {"representative": True, "owner": True, "executive": True,
                         "title": "CEO", "percent_ownership": "100"},
    })
    call(key, "POST", f"/v1/accounts/{account_id}/external_accounts", form={
        "external_account[object]": "bank_account", "external_account[country]": "US",
        "external_account[currency]": "usd",
        "external_account[account_holder_name]": "Northwind Outfitters",
        "external_account[account_holder_type]": "company",
        "external_account[routing_number]": "110000000",
        "external_account[account_number]": "000123456789",
        "default_for_currency": "true",
    })
    for _ in range(18):  # verification of test values is asynchronous (~1 min)
        caps = call(key, "GET", f"/v2/core/accounts/{account_id}?include=configuration.merchant",
                    )["configuration"]["merchant"]["capabilities"]
        if (caps["card_payments"]["status"] == "active"
                and caps["stripe_balance"]["payouts"]["status"] == "active"):
            print(f"RECON_STRIPE_ACCOUNT={account_id}")
            return
        time.sleep(10)
    raise SystemExit(f"{account_id} created but not active yet; re-check in the dashboard")


if __name__ == "__main__":
    main()
