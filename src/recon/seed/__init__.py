"""Demo-data seeding through the real sandbox APIs (see docs/data_model.md section 7)."""

from __future__ import annotations

import csv
import json
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

from recon.seed.plan import OpenInvoice, Scenario, expected_matches
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
