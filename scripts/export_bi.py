"""Export the close dashboard's data for the BI dashboard (Tableau Public) as two flat CSV files.

    uv run recon demo                       # builds demo.duckdb from the committed snapshot
    uv run python scripts/export_bi.py # writes bi/*.csv

cash_items.csv has one row per customer payment (Stripe payouts are excluded because they
are internal transfers, the same scope as the 58 payments in the README). close_status.csv
holds the one-row close summary. Ages are counted from the latest payment in the data
(the "as of" date), not from today, so the same snapshot always gives the same file.
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

import duckdb

from recon.app.ui import CODES, TIER_LABEL

STATUS_LABEL = {"auto_matched": "Auto-matched", "matched_needs_review": "Matched, needs review",
                "exception": "Unmatched"}
BUCKETS = ((3, "0-3 days"), (7, "4-7 days"), (14, "8-14 days"))  # else "15+ days"

ITEMS_SQL = """
    -- A payment can have several decision rows (one per invoice it pays). Aggregate
    -- explicitly so the result never depends on row order: the worst status wins, and
    -- tier and exception code come from the rows that carry that worst status, so a
    -- payment is never shown as "Unmatched" with a tier-1 match.
    with rows as (
        select *, case status when 'exception' then 3
                              when 'matched_needs_review' then 2 else 1 end as severity
        from marts.fct_cash_decisions
        where source <> 'stripe_payout'
    ), ranked as (
        select *, max(severity) over (partition by source, source_id) as worst
        from rows
    )
    select source, source_id, max(cash_date) as cash_date,
           max(amount_cents) as amount_cents,
           string_agg(distinct partner_name, ' / ' order by partner_name) as customer,
           ['auto_matched', 'matched_needs_review', 'exception'][max(severity)] as status,
           min(tier) filter (where severity = worst) as tier,
           min(exception_code) filter (where severity = worst) as exception_code
    from ranked
    group by source, source_id
    order by cash_date, source, source_id
"""


def bucket(days: int) -> str:
    return next((label for limit, label in BUCKETS if days <= limit), "15+ days")


def export(db: Path, out_dir: Path) -> int:
    con = duckdb.connect(str(db), read_only=True)
    rows = con.execute(ITEMS_SQL).fetchall()
    cur = con.execute("select * from marts.rpt_close_status")
    cols = [d[0] for d in cur.description]
    summary = dict(zip(cols, cur.fetchone() or (), strict=True))
    # The dashboard must tie to the close summary: same payments, same cash, to the cent.
    total = sum(r[3] for r in rows)
    if (len(rows), total) != (summary["cash_items"], summary["cash_cents"]):
        raise SystemExit(f"export does not tie to rpt_close_status: {len(rows)} payments / "
                         f"{total} cents vs {summary['cash_items']} / {summary['cash_cents']}")
    as_of = max(r[2] for r in rows)
    out_dir.mkdir(parents=True, exist_ok=True)

    with (out_dir / "cash_items.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["source", "source_id", "cash_date", "amount_usd", "customer", "status",
                    "match_tier", "exception_code", "exception_reason", "severity",
                    "as_of_date", "age_days", "aging_bucket"])
        for source, sid, cash_date, cents, customer, status, tier, code in rows:
            label, severity, *_ = CODES.get(code, (code, "", "", "")) if code else ("", "", "")
            tier_label = f"Tier {tier} · {TIER_LABEL[tier]}" if tier else "Not matched"
            age = (as_of - cash_date).days
            w.writerow([source, sid, cash_date, f"{cents / 100:.2f}", customer or "",
                        STATUS_LABEL[status], tier_label, code or "", label, severity,
                        as_of, age, bucket(age)])

    with (out_dir / "close_status.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        keep = [c for c in cols if c != "matched_at"]  # run timestamp: changes every build
        w.writerow([c.replace("_cents", "_usd") for c in keep])
        w.writerow([f"{summary[c] / 100:.2f}" if c.endswith("_cents") else summary[c]
                    for c in keep])
    return len(rows)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--db", type=Path, default=Path("demo.duckdb"))
    p.add_argument("--out", type=Path, default=Path("bi"))
    a = p.parse_args()
    n = export(a.db, a.out)
    print(f"{n} payments -> {a.out}/cash_items.csv, close summary -> {a.out}/close_status.csv")


if __name__ == "__main__":
    main()
