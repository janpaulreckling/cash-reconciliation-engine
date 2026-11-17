"""Load matching inputs from the dbt models and persist decisions (audit trail)."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict
from datetime import UTC, date, datetime

import duckdb

from recon.matching.engine import Cash, Decision, Invoice
from recon.matching.payouts import Deposit, Payout

DDL = """
CREATE SCHEMA IF NOT EXISTS engine;  -- not "recon": that is the DuckDB catalog name
CREATE TABLE IF NOT EXISTS engine.match_runs (
    run_id VARCHAR PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL,
    config JSON NOT NULL,
    decisions INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS engine.match_decisions (
    run_id VARCHAR NOT NULL,
    source VARCHAR NOT NULL,          -- stripe_charge | bank_receipt | stripe_payout
    source_id VARCHAR NOT NULL,
    tier INTEGER,                     -- 1 exact, 2 fuzzy, 3 one-to-many, NULL = not placed
    score DOUBLE NOT NULL,
    rule VARCHAR,
    invoice_number VARCHAR,           -- one row per linked invoice
    applied_cents BIGINT,
    writeoff_cents BIGINT,
    exception_code VARCHAR,
    suggestion JSON,
    breakdown JSON
);
CREATE OR REPLACE VIEW engine.current_decisions AS
SELECT d.* FROM engine.match_decisions d
WHERE d.run_id = (SELECT run_id FROM engine.match_runs ORDER BY started_at DESC LIMIT 1);
"""


def load_invoices(con: duckdb.DuckDBPyConnection) -> list[Invoice]:
    rows = con.execute("""
        SELECT invoice_number, partner_id, partner_name, invoice_date, due_date,
               amount_residual_cents
        FROM staging.stg_erp__invoices
        WHERE move_type = 'out_invoice' AND amount_residual_cents > 0
    """).fetchall()
    return [Invoice(*r) for r in rows]


def load_cash(con: duckdb.DuckDBPyConnection) -> list[Cash]:
    charges = con.execute("""
        SELECT c.charge_id, c.created_at::DATE, c.amount_cents, c.amount_refunded_cents,
               c.is_disputed, c.invoice_ref, c.description, c.billing_name
        FROM staging.stg_stripe__charges c
    """).fetchall()
    cash: list[Cash] = []
    for cid, day, amount, refunded, disputed, ref, desc, payer in charges:
        flags = set()
        if disputed:
            flags.add("DISPUTED")
        if refunded >= amount:
            flags.add("REFUNDED")
        elif refunded > 0:
            flags.add("PARTIAL_REFUND")
        net = 0 if disputed else amount - refunded
        cash.append(Cash("stripe_charge", cid, day, amount, net, ref, desc, payer,
                         frozenset(flags)))
    receipts = con.execute("""
        SELECT bank_transaction_id, posted_date, amount_cents, description
        FROM intermediate.int_bank__classified
        WHERE cash_class = 'customer_receipt' AND in_close_period
    """).fetchall()
    cash += [Cash("bank_receipt", rid, day, amt, amt, None, desc, None)
             for rid, day, amt, desc in receipts]
    return cash


def load_payouts(con: duckdb.DuckDBPyConnection) -> tuple[list[Payout], list[Deposit]]:
    payouts = [Payout(*r) for r in con.execute("""
        SELECT p.payout_id, p.payout_cents, p.arrival_date, s.status, p.automatic, s.trace_id,
               p.component_count, p.components_net_cents
        FROM intermediate.int_stripe__payout_components p
        JOIN staging.stg_stripe__payouts s USING (payout_id)
    """).fetchall()]
    deposits = [Deposit(*r) for r in con.execute("""
        SELECT bank_transaction_id, posted_date, amount_cents, description
        FROM intermediate.int_bank__classified WHERE cash_class = 'stripe_payout'
    """).fetchall()]
    return payouts, deposits


def save(con: duckdb.DuckDBPyConnection, decisions: list[Decision], config: object) -> str:
    con.execute(DDL)
    run_id = uuid.uuid4().hex
    rows = []
    for d in decisions:
        links: list[tuple[str | None, int | None]] = list(
            zip(d.invoices, d.applied_cents, strict=True)) or [(None, None)]
        for inv, applied in links:
            rows.append([run_id, d.source, d.source_id, d.tier, d.score, d.rule, inv, applied,
                         d.writeoff_cents if inv else None, d.exception,
                         json.dumps(d.suggestion), json.dumps(d.breakdown)])
    con.execute("BEGIN")
    try:
        con.execute("INSERT INTO engine.match_runs VALUES (?, ?, ?, ?)",
                    [run_id, datetime.now(UTC), json.dumps(asdict(config), default=str),  # type: ignore[call-overload]
                     len(decisions)])
        if rows:
            con.executemany("INSERT INTO engine.match_decisions VALUES"
                            " (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return run_id


def today() -> date:
    return datetime.now(UTC).date()
