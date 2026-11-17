"""Demo-data seeding through the real sandbox APIs (see docs/data_model.md section 7)."""

from __future__ import annotations

import csv
import json
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

from recon.seed.plan import Case, Channel, OpenInvoice, Scenario, expected_matches
from recon.warehouse import Warehouse

SEEDS_DIR = Path("seeds")

# Latest version per invoice FIRST, then filter (see docs/data_model.md section 4).
OPEN_INVOICES_SQL = """
WITH latest AS (
    SELECT * FROM raw.erp_invoices
    QUALIFY row_number() OVER (PARTITION BY id ORDER BY write_date DESC, _loaded_at DESC) = 1
)
SELECT id, name, partner_id, partner_name, ref, amount_residual
FROM latest
WHERE move_type = 'out_invoice' AND state = 'posted' AND currency = 'USD'
  AND payment_state IN ('not_paid', 'partial') AND amount_residual > 0
ORDER BY name
"""


def open_invoices(wh: Warehouse) -> list[OpenInvoice]:
    return [
        OpenInvoice(id=r[0], name=r[1], partner_id=r[2], partner_name=r[3], opportunity_id=r[4],
                    residual_cents=int(Decimal(r[5]) * 100))
        for r in wh.con.execute(OPEN_INVOICES_SQL).fetchall()
    ]


def write_plan(plan: list[Scenario], seeds_dir: Path = SEEDS_DIR) -> None:
    seeds_dir.mkdir(parents=True, exist_ok=True)
    (seeds_dir / "scenario_plan.json").write_text(json.dumps(
        [{**asdict(s), "case": s.case.value, "channel": s.channel.value} for s in plan],
        indent=2))
    rows = expected_matches(plan)
    with (seeds_dir / "expected_matches.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def load_plan(seeds_dir: Path = SEEDS_DIR) -> list[Scenario]:
    """The committed plan is canonical: later seed steps must not re-derive it from a
    changed ERP state."""
    raw = json.loads((seeds_dir / "scenario_plan.json").read_text())
    return [Scenario(case_id=s["case_id"], case=Case(s["case"]), channel=Channel(s["channel"]),
                     invoices=[OpenInvoice(**i) for i in s["invoices"]],
                     pay_cents=s["pay_cents"], memo=s["memo"], extra=s["extra"])
            for s in raw]


def write_cash_truth(wh: Warehouse, plan: list[Scenario], stripe_ledger: Path,
                     seeds_dir: Path = SEEDS_DIR) -> list[dict[str, str]]:
    """Which real cash item belongs to which scenario, as created in the sandboxes.

    Card cases: the seed ledger knows the PaymentIntents; Stripe's charge carries the
    PaymentIntent id. Bank cases: the seeded line is regenerated deterministically and
    found by exact description and amount. Every scenario with cash must resolve to
    exactly the expected number of items, or this fails loudly.
    """
    from recon.seed.plaid_seed import transfer_lines

    ledger = json.loads(stripe_ledger.read_text())["cases"]
    charge_of = dict(wh.con.execute(
        "SELECT payment_intent, id FROM raw.stripe_charges").fetchall())
    invoice_dates = dict(wh.con.execute(
        "SELECT name, invoice_date FROM raw.erp_invoices WHERE invoice_date IS NOT NULL"
    ).fetchall())
    from datetime import date

    lines = {ln.key: ln for ln in transfer_lines(plan, date(2026, 11, 2), date(2026, 11, 30),
                                                 invoice_dates)}
    rows: list[dict[str, str]] = []
    for s in plan:
        if s.channel is Channel.CARD:
            for pi in ledger[s.case_id]["payment_intents"]:
                rows.append({"case_id": s.case_id, "source": "stripe_charge",
                             "source_id": charge_of[pi]})
        elif s.channel is Channel.TRANSFER:
            ln = lines[f"transfer:{s.case_id}"]
            hits = wh.con.execute(
                "SELECT DISTINCT id FROM raw.bank_transactions WHERE NOT removed"
                " AND coalesce(original_description, name) = ? AND amount = ?",
                [ln.description, ln.amount_cents / 100]).fetchall()  # both Plaid-signed
            if len(hits) != 1:
                raise RuntimeError(f"{s.case_id}: expected 1 bank line, found {len(hits)}")
            rows.append({"case_id": s.case_id, "source": "bank_receipt", "source_id": hits[0][0]})
    with (seeds_dir / "expected_cash.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["case_id", "source", "source_id"])
        writer.writeheader()
        writer.writerows(rows)
    return rows
