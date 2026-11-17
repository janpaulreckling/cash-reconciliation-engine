"""Committed snapshot of the raw layer, so everything downstream runs without sandbox keys.

`export` writes every raw table (all record versions, load metadata included) to
`seeds/snapshot/*.csv` from one real seeded run. `load` rebuilds a fresh warehouse from
those files. CI and the public demo use it: snapshot -> dbt -> match -> evaluate, with
no Odoo, Stripe or Plaid access and no secrets.
"""

from __future__ import annotations

from pathlib import Path

from recon.warehouse import RAW_TABLES, Warehouse

SNAPSHOT_DIR = Path("seeds/snapshot")
META = ("_row_hash", "_loaded_at", "_source_run_id")


def export(wh: Warehouse, out_dir: Path = SNAPSHOT_DIR) -> dict[str, int]:
    out_dir.mkdir(parents=True, exist_ok=True)
    counts = {}
    for table, cols in RAW_TABLES.items():
        names = ", ".join([c for c, _ in cols] + list(META))
        path = out_dir / f"{table}.csv"
        wh.con.execute(
            f"COPY (SELECT {names} FROM raw.{table} ORDER BY id, _loaded_at)"
            f" TO '{path}' (HEADER, DELIMITER ',')")
        row = wh.con.execute(f"SELECT count(*) FROM raw.{table}").fetchone()
        counts[table] = int(row[0]) if row else 0
    return counts


def load(wh: Warehouse, in_dir: Path = SNAPSHOT_DIR) -> dict[str, int]:
    """Load into an empty warehouse. Refuses to mix with existing data."""
    counts = {}
    for table, cols in RAW_TABLES.items():
        existing = wh.con.execute(f"SELECT count(*) FROM raw.{table}").fetchone()
        if existing and existing[0]:
            raise RuntimeError(f"raw.{table} is not empty; load the snapshot into a new file")
        path = in_dir / f"{table}.csv"
        if not path.exists():
            counts[table] = 0
            continue
        names = [c for c, _ in cols] + list(META)
        types = {c: t.replace(" NOT NULL", "") for c, t in cols}
        types |= {"_row_hash": "VARCHAR", "_loaded_at": "TIMESTAMPTZ",
                  "_source_run_id": "VARCHAR"}
        col_types = ", ".join(f"'{n}': '{types[n]}'" for n in names)
        wh.con.execute(
            f"INSERT INTO raw.{table} ({', '.join(names)}) SELECT {', '.join(names)}"
            f" FROM read_csv('{path}', header = true, columns = {{{col_types}}})")
        row = wh.con.execute(f"SELECT count(*) FROM raw.{table}").fetchone()
        counts[table] = int(row[0]) if row else 0
    return counts


def fingerprint(in_dir: Path = SNAPSHOT_DIR) -> str:
    """Changes whenever anything that shapes the demo's numbers changes: the snapshot,
    the matching config, the matching engine or the dbt project. A demo warehouse
    built from older inputs is then recognized as stale and rebuilt."""
    import hashlib

    inputs = [*in_dir.glob("*.csv"), Path("config/matching.yaml"),
              *Path("src/recon/matching").glob("*.py"),
              *(p for p in Path("dbt").rglob("*")
                if p.suffix in {".sql", ".yml", ".csv"} and "target" not in p.parts)]
    h = hashlib.sha256()
    for f in sorted(inputs):
        h.update(f.name.encode())
        h.update(f.read_bytes())
    return h.hexdigest()
