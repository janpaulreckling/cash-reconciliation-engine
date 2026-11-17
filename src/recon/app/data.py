"""Read side of the review app, plus the one thing it writes: review decisions."""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime
from typing import Any

import duckdb

from recon.warehouse import Warehouse


def db_path() -> str:
    """The real warehouse when running with sandbox credentials (.env), otherwise the
    demo warehouse rebuilt from the committed snapshot (e.g. Streamlit Cloud)."""
    from pathlib import Path

    if "RECON_DUCKDB_PATH" in os.environ:
        return os.environ["RECON_DUCKDB_PATH"]
    return "recon.duckdb" if Path(".env").exists() else "demo.duckdb"


def ensure_demo_db() -> None:
    """Public demo (Streamlit Community Cloud): no sandbox keys there, so the first visit
    builds the warehouse from the committed snapshot (`recon demo`: load -> dbt -> match).

    The build runs in a child process under a temporary name: when that process exits,
    every DuckDB connection (dbt's included) is closed and checkpointed, so renaming the
    finished file can't lose writes still sitting in the WAL, and visitors never see a
    half-built warehouse. The app calls this once per process (st.cache_resource)."""
    import subprocess
    import sys
    from pathlib import Path

    target = Path(db_path())
    stamp = target.with_name(target.name + ".snapshot")
    if target.exists() and not stamp.exists() and target.name != "demo.duckdb":
        return  # a real warehouse (local use), not a demo build: never touch it
    if target.exists() and stamp.read_text() == snapshot_fingerprint():
        return
    target.unlink(missing_ok=True)  # demo built from an older snapshot: rebuild
    building = target.with_name(target.name + ".building")
    for leftover in (building, building.with_name(building.name + ".wal")):
        leftover.unlink(missing_ok=True)
    subprocess.run([sys.executable, "-m", "recon.cli", "demo", "--db", str(building)],
                   check=True, env={**os.environ, "RECON_DUCKDB_PATH": str(building)})
    building.rename(target)
    stamp.write_text(snapshot_fingerprint())


def snapshot_fingerprint() -> str:
    """Changes whenever the committed snapshot or the matching config changes."""
    import hashlib
    from pathlib import Path

    h = hashlib.sha256()
    for f in sorted([*Path("seeds/snapshot").glob("*.csv"), Path("config/matching.yaml")]):
        h.update(f.name.encode())
        h.update(f.read_bytes())
    return h.hexdigest()


def _query(sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
    # Short-lived connections, so the CLI can run while the app is open. Not read_only:
    # DuckDB requires one configuration per file within a process, and the demo
    # bootstrap's dbt run holds a read-write connection in this process.
    con = duckdb.connect(db_path())
    try:
        cur = con.execute(sql, params or [])
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]
    finally:
        con.close()


def close_status() -> dict[str, Any]:
    rows = _query("SELECT * FROM marts.rpt_close_status")
    return rows[0] if rows else {}


def exceptions() -> list[dict[str, Any]]:
    rows = _query("""
        SELECT e.*, r.action AS review_action, r.comment AS review_comment,
               r.reviewer, r.decided_at
        FROM marts.fct_exceptions e
        LEFT JOIN (
            SELECT * FROM engine.reviews
            QUALIFY row_number() OVER (PARTITION BY source, source_id
                                       ORDER BY decided_at DESC) = 1
        ) r USING (source, source_id)
        ORDER BY e.priority, e.amount_cents DESC
    """)
    for r in rows:
        r["suggestion"] = json.loads(r["suggestion"] or "[]")
        r["breakdown"] = json.loads(r["breakdown"] or "{}")
    return rows


def invoices(numbers: list[str]) -> list[dict[str, Any]]:
    if not numbers:
        return []
    return _query("""
        SELECT invoice_number, partner_name, invoice_date, due_date, amount_total_cents,
               amount_residual_cents, payment_state, opportunity_id
        FROM staging.stg_erp__invoices WHERE invoice_number IN (SELECT unnest(?))
        ORDER BY invoice_number
    """, [numbers])


def matched() -> list[dict[str, Any]]:
    return _query("""
        SELECT source, source_id, cash_date, amount_cents, cash_text, tier, score, rule,
               invoice_number, partner_name, applied_cents, writeoff_cents, status
        FROM marts.fct_cash_decisions
        WHERE tier IS NOT NULL AND source <> 'stripe_payout'
        ORDER BY cash_date DESC, source_id
    """)


def journal() -> list[dict[str, Any]]:
    return _query("""
        SELECT entry_id, entry_date, account, account_name, debit_cents, credit_cents, memo
        FROM marts.fct_journal_suggestions ORDER BY entry_date, entry_id, debit_cents DESC
    """)


def payouts() -> list[dict[str, Any]]:
    return _query("""
        SELECT p.payout_id, p.amount_cents, p.arrival_date, p.status, c.component_count,
               c.components_net_cents, d.exception_code, d.tier
        FROM staging.stg_stripe__payouts p
        LEFT JOIN intermediate.int_stripe__payout_components c USING (payout_id)
        LEFT JOIN marts.fct_cash_decisions d
               ON d.source = 'stripe_payout' AND d.source_id = p.payout_id
        ORDER BY p.arrival_date DESC
    """)


def latest_run_id() -> str | None:
    rows = _query("SELECT run_id FROM engine.match_runs ORDER BY started_at DESC LIMIT 1")
    return str(rows[0]["run_id"]) if rows else None


def record_review(*, source: str, source_id: str, action: str, invoice_number: str | None,
                  comment: str, reviewer: str) -> None:
    run_id = latest_run_id() or "none"
    wh = Warehouse(db_path())
    try:
        wh.con.execute(
            "INSERT INTO engine.reviews VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [uuid.uuid4().hex, run_id, source, source_id, action, invoice_number,
             comment.strip() or None, reviewer, datetime.now(UTC)])
    finally:
        wh.close()
