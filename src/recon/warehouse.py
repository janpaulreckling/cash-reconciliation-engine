"""DuckDB warehouse: append-only raw tables plus an extract run log.

Raw tables keep every *version* of a source record as it was pulled, stamped with
`_row_hash`, `_loaded_at` and `_source_run_id`. A version is identified by the record's
key plus a hash of its business columns, so

- re-reading an unchanged record (watermark overlap, full re-pull) stores nothing,
- any change, even one without a timestamp (Stripe objects have none), becomes a new row,
- an auditor can see what the source said at each point in time.

Deduplication to the latest version happens in dbt staging.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import duckdb

Column = tuple[str, str]

# Column order is the INSERT order. ERP money stays in source units (DECIMAL);
# Stripe money is already integer minor units (cents). Staging normalizes both.
RAW_TABLES: dict[str, list[Column]] = {
    "erp_invoices": [
        ("id", "BIGINT NOT NULL"),
        ("name", "VARCHAR NOT NULL"),
        ("move_type", "VARCHAR NOT NULL"),
        ("state", "VARCHAR NOT NULL"),
        ("partner_id", "BIGINT"),
        ("partner_name", "VARCHAR"),
        ("invoice_origin", "VARCHAR"),
        ("ref", "VARCHAR"),
        ("payment_reference", "VARCHAR"),
        ("invoice_date", "DATE"),
        ("invoice_date_due", "DATE"),
        ("currency", "VARCHAR"),
        ("amount_total", "DECIMAL(18, 2) NOT NULL"),
        ("amount_residual", "DECIMAL(18, 2) NOT NULL"),
        ("payment_state", "VARCHAR"),
        ("reversed_entry_id", "BIGINT"),
        ("write_date", "TIMESTAMPTZ NOT NULL"),
    ],
    "stripe_charges": [
        ("id", "VARCHAR NOT NULL"),
        ("payment_intent", "VARCHAR"),
        ("amount", "BIGINT NOT NULL"),
        ("amount_captured", "BIGINT"),
        ("amount_refunded", "BIGINT"),
        ("currency", "VARCHAR NOT NULL"),
        ("created", "TIMESTAMPTZ NOT NULL"),
        ("status", "VARCHAR"),
        ("paid", "BOOLEAN"),
        ("refunded", "BOOLEAN"),
        ("disputed", "BOOLEAN"),
        ("customer", "VARCHAR"),
        ("billing_email", "VARCHAR"),
        ("billing_name", "VARCHAR"),
        ("description", "VARCHAR"),
        ("metadata", "JSON"),
        ("balance_transaction", "VARCHAR"),
    ],
    "stripe_refunds": [
        ("id", "VARCHAR NOT NULL"),
        ("charge", "VARCHAR"),
        ("payment_intent", "VARCHAR"),
        ("amount", "BIGINT NOT NULL"),
        ("currency", "VARCHAR NOT NULL"),
        ("created", "TIMESTAMPTZ NOT NULL"),
        ("status", "VARCHAR"),
        ("reason", "VARCHAR"),
        ("balance_transaction", "VARCHAR"),
    ],
    "stripe_disputes": [
        ("id", "VARCHAR NOT NULL"),
        ("charge", "VARCHAR"),
        ("payment_intent", "VARCHAR"),
        ("amount", "BIGINT NOT NULL"),
        ("currency", "VARCHAR NOT NULL"),
        ("created", "TIMESTAMPTZ NOT NULL"),
        ("status", "VARCHAR"),
        ("reason", "VARCHAR"),
    ],
    "stripe_balance_transactions": [
        ("id", "VARCHAR NOT NULL"),
        ("type", "VARCHAR NOT NULL"),
        ("reporting_category", "VARCHAR"),
        ("source", "VARCHAR"),
        ("amount", "BIGINT NOT NULL"),
        ("fee", "BIGINT NOT NULL"),
        ("net", "BIGINT NOT NULL"),
        ("currency", "VARCHAR NOT NULL"),
        ("created", "TIMESTAMPTZ NOT NULL"),
        ("available_on", "TIMESTAMPTZ"),
        ("status", "VARCHAR"),
        ("description", "VARCHAR"),
        ("payout", "VARCHAR"),  # set once the transaction has been paid out
    ],
    "bank_transactions": [  # Plaid shape; Plaid amounts are positive for money OUT
        ("id", "VARCHAR NOT NULL"),  # Plaid transaction_id
        ("account_id", "VARCHAR NOT NULL"),
        ("date", "DATE NOT NULL"),
        ("authorized_date", "DATE"),
        ("amount", "DECIMAL(18, 2) NOT NULL"),
        ("iso_currency_code", "VARCHAR"),
        ("name", "VARCHAR"),
        ("merchant_name", "VARCHAR"),
        ("original_description", "VARCHAR"),
        ("payment_channel", "VARCHAR"),
        ("category", "VARCHAR"),
        ("category_detailed", "VARCHAR"),
        ("pending", "BOOLEAN"),
        ("removed", "BOOLEAN NOT NULL"),  # Plaid sync reported it as removed
    ],
    "bank_accounts": [
        ("id", "VARCHAR NOT NULL"),  # Plaid account_id
        ("name", "VARCHAR"),
        ("mask", "VARCHAR"),
        ("type", "VARCHAR"),
        ("subtype", "VARCHAR"),
        ("iso_currency_code", "VARCHAR"),
    ],
    "stripe_payouts": [
        ("id", "VARCHAR NOT NULL"),
        ("amount", "BIGINT NOT NULL"),
        ("currency", "VARCHAR NOT NULL"),
        ("created", "TIMESTAMPTZ NOT NULL"),
        ("arrival_date", "TIMESTAMPTZ"),
        ("status", "VARCHAR"),
        ("method", "VARCHAR"),
        ("automatic", "BOOLEAN"),
        ("statement_descriptor", "VARCHAR"),
        ("trace_id", "VARCHAR"),
        ("balance_transaction", "VARCHAR"),
        ("failure_code", "VARCHAR"),
    ],
}

META_DDL = """
CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS meta;
CREATE TABLE IF NOT EXISTS meta.source_state (
    source VARCHAR NOT NULL,
    key VARCHAR NOT NULL,
    value VARCHAR,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (source, key)
);
CREATE TABLE IF NOT EXISTS meta.extract_runs (
    run_id VARCHAR PRIMARY KEY,
    source VARCHAR NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    status VARCHAR NOT NULL,          -- running | succeeded | failed
    rows_loaded INTEGER,
    watermark_from TIMESTAMPTZ,
    watermark_to TIMESTAMPTZ,
    error VARCHAR
);
"""


def row_hash(table: str, row: Mapping[str, Any]) -> str:
    """Stable hash of a record's business columns (not the load metadata)."""

    def canon(value: Any) -> Any:
        if isinstance(value, datetime):
            return value.astimezone(UTC).isoformat()
        if isinstance(value, date | Decimal):
            return str(value)
        return value

    payload = {name: canon(row[name]) for name, _ in RAW_TABLES[table]}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class RunResult:
    run_id: str
    source: str
    rows_loaded: dict[str, int]
    watermark_to: datetime | None

    @property
    def total(self) -> int:
        return sum(self.rows_loaded.values())


class Warehouse:
    def __init__(self, path: str) -> None:
        self.con = duckdb.connect(path)
        self.con.execute("SET TimeZone = 'UTC'")
        self.con.execute(META_DDL)
        for table, cols in RAW_TABLES.items():
            col_sql = ", ".join(f"{name} {type_}" for name, type_ in cols)
            self.con.execute(
                f"CREATE TABLE IF NOT EXISTS raw.{table} ({col_sql},"
                " _row_hash VARCHAR NOT NULL, _loaded_at TIMESTAMPTZ NOT NULL,"
                " _source_run_id VARCHAR NOT NULL)"
            )
            # Additive schema evolution: new source columns are added as nullable.
            # Older versions keep NULL there; the next pull stores a new version.
            for name, type_ in cols:
                self.con.execute(
                    f"ALTER TABLE raw.{table} ADD COLUMN IF NOT EXISTS {name}"
                    f" {type_.replace(' NOT NULL', '')}"
                )

    def close(self) -> None:
        self.con.close()

    def watermark(self, source: str) -> datetime | None:
        row = self.con.execute(
            "SELECT max(watermark_to) FROM meta.extract_runs"
            " WHERE source = ? AND status = 'succeeded'",
            [source],
        ).fetchone()
        return row[0] if row and row[0] is not None else None

    @contextmanager
    def _run(self, source: str, since: datetime | None) -> Iterator[str]:
        run_id = uuid.uuid4().hex
        self.con.execute(
            "INSERT INTO meta.extract_runs (run_id, source, started_at, status, watermark_from)"
            " VALUES (?, ?, ?, 'running', ?)",
            [run_id, source, datetime.now(UTC), since],
        )
        try:
            yield run_id
        except Exception as exc:
            self.con.execute(
                "UPDATE meta.extract_runs SET status = 'failed', finished_at = ?, error = ?"
                " WHERE run_id = ?",
                [datetime.now(UTC), f"{type(exc).__name__}: {exc}", run_id],
            )
            raise

    def load(
        self,
        source: str,
        batches: Mapping[str, Iterable[dict[str, Any]]],
        *,
        since: datetime | None = None,
        cursor: tuple[str, str] | None = None,
        state: Mapping[str, str] | None = None,
    ) -> RunResult:
        """Append new record versions to one or more raw tables in a single transaction.

        Either every table of the run lands and the run is marked succeeded, or nothing
        does and the watermark stays where it was. `cursor=(table, field)` advances the
        watermark to the highest value of that field; without it the source is pulled
        in full each time and only changed versions are stored. `state` (e.g. an API
        sync cursor) is saved in the same transaction, so it can never run ahead of
        the rows it covers.
        """
        with self._run(source, since) as run_id:
            # Materialize first: a source failure mid-iteration must not leave a
            # partial load behind.
            pulled = {table: list(rows) for table, rows in batches.items()}
            high = since
            if cursor is not None:
                table, field = cursor
                high = max((r[field] for r in pulled[table]), default=since)

            loaded_at = datetime.now(UTC)
            counts: dict[str, int] = {}
            self.con.execute("BEGIN")
            try:
                for table, rows in pulled.items():
                    counts[table] = self._insert_new_versions(table, rows, loaded_at, run_id)
                for key, value in (state or {}).items():
                    self.con.execute(
                        "INSERT OR REPLACE INTO meta.source_state VALUES (?, ?, ?, ?)",
                        [source, key, value, loaded_at],
                    )
                self.con.execute(
                    "UPDATE meta.extract_runs SET status = 'succeeded', finished_at = ?,"
                    " rows_loaded = ?, watermark_to = ? WHERE run_id = ?",
                    [datetime.now(UTC), sum(counts.values()), high, run_id],
                )
                self.con.execute("COMMIT")
            except Exception:
                self.con.execute("ROLLBACK")
                raise
        return RunResult(run_id, source, counts, high)

    def _insert_new_versions(
        self, table: str, rows: list[dict[str, Any]], loaded_at: datetime, run_id: str
    ) -> int:
        if not rows:
            return 0
        columns = [name for name, _ in RAW_TABLES[table]]
        ids = list({r["id"] for r in rows})
        stored = {
            (k, h)
            for k, h in self.con.execute(
                f"SELECT id, _row_hash FROM raw.{table} WHERE id IN (SELECT unnest(?))", [ids]
            ).fetchall()
        }
        batch: list[list[Any]] = []
        for r in rows:
            h = row_hash(table, r)
            if (r["id"], h) in stored:
                continue
            stored.add((r["id"], h))  # same version twice in one pull
            values = [json.dumps(r[c]) if c == "metadata" else r[c] for c in columns]
            batch.append([*values, h, loaded_at, run_id])
        if batch:
            placeholders = ", ".join("?" for _ in range(len(columns) + 3))
            self.con.executemany(
                f"INSERT INTO raw.{table} ({', '.join(columns)}, _row_hash, _loaded_at,"
                f" _source_run_id) VALUES ({placeholders})",
                batch,
            )
        return len(batch)

    def tombstones(self, table: str, ids: list[str]) -> list[dict[str, Any]]:
        """Latest stored version of each id, marked removed (for sources that report
        deletions by id only). Ids never seen before are ignored."""
        if not ids:
            return []
        columns = [name for name, _ in RAW_TABLES[table]]
        rows = self.con.execute(
            f"SELECT {', '.join(columns)} FROM raw.{table} WHERE id IN (SELECT unnest(?))"
            " QUALIFY row_number() OVER (PARTITION BY id ORDER BY _loaded_at DESC) = 1",
            [ids],
        ).fetchall()
        return [{**dict(zip(columns, r, strict=True)), "removed": True} for r in rows]

    def state(self, source: str, key: str) -> str | None:
        row = self.con.execute(
            "SELECT value FROM meta.source_state WHERE source = ? AND key = ?", [source, key]
        ).fetchone()
        return str(row[0]) if row and row[0] is not None else None

    def recent_runs(self, limit: int = 10) -> list[tuple[Any, ...]]:
        return self.con.execute(
            "SELECT source, started_at, status, rows_loaded, watermark_to, error"
            " FROM meta.extract_runs ORDER BY started_at DESC LIMIT ?",
            [limit],
        ).fetchall()

    def row_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for table in RAW_TABLES:
            row = self.con.execute(f"SELECT count(*) FROM raw.{table}").fetchone()
            counts[table] = int(row[0]) if row else 0
        return counts
