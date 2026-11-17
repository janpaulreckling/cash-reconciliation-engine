"""Command-line entry point: `recon extract erp`, `recon status`."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from recon.config import Settings
from recon.errors import ReconError
from recon.sources.odoo import OdooInvoiceSource, XmlRpcOdoo
from recon.sources.plaid_source import PlaidClient, PlaidSource
from recon.sources.stripe_source import StripeClientAPI, StripeSource
from recon.warehouse import Warehouse

app = typer.Typer(help="Cash reconciliation engine (Stripe / bank / ERP).", no_args_is_help=True)
extract_app = typer.Typer(help="Pull source data into raw DuckDB tables.", no_args_is_help=True)
app.add_typer(extract_app, name="extract")
seed_app = typer.Typer(help="Create demo data in the sandboxes (test mode only).",
                       no_args_is_help=True)
app.add_typer(seed_app, name="seed")
console = Console()


@extract_app.command("erp")
def extract_erp(
    full: bool = typer.Option(False, "--full", help="Ignore the watermark and re-pull everything."),
) -> None:
    """Posted customer invoices and credit notes from Odoo (read-only)."""
    settings = Settings()
    wh = Warehouse(settings.duckdb_path)
    try:
        since = None if full else wh.watermark("odoo_invoices")
        rpc = XmlRpcOdoo(settings.odoo_url, settings.odoo_db, settings.odoo_username,
                         settings.odoo_password.get_secret_value())
        result = wh.load("odoo_invoices", {"erp_invoices": OdooInvoiceSource(rpc).fetch(since)},
                         since=since, cursor=("erp_invoices", "write_date"))
    except ReconError as exc:
        console.print(f"[red]Extract failed:[/red] {exc}")
        raise typer.Exit(1) from exc
    finally:
        wh.close()
    console.print(f"odoo_invoices: {result.total} new versions "
                  f"(since {since or 'beginning'}, watermark now {result.watermark_to})")


@extract_app.command("stripe")
def extract_stripe() -> None:
    """Charges, refunds, disputes, balance transactions and payouts (test mode only)."""
    settings = Settings()
    if settings.stripe_api_key is None:
        console.print("[red]RECON_STRIPE_API_KEY is not set (see .env.example)[/red]")
        raise typer.Exit(1)
    wh = Warehouse(settings.duckdb_path)
    try:
        api = StripeClientAPI(settings.stripe_api_key.get_secret_value(),
                              settings.stripe_account)
        result = wh.load("stripe", StripeSource(api).fetch())
    except ReconError as exc:
        console.print(f"[red]Extract failed:[/red] {exc}")
        raise typer.Exit(1) from exc
    finally:
        wh.close()
    for table, n in result.rows_loaded.items():
        console.print(f"{table}: {n} new versions")


def _plaid_client(settings: Settings) -> PlaidClient:
    if settings.plaid_client_id is None or settings.plaid_secret is None:
        console.print("[red]RECON_PLAID_CLIENT_ID / RECON_PLAID_SECRET are not set[/red]")
        raise typer.Exit(1)
    return PlaidClient(settings.plaid_client_id, settings.plaid_secret.get_secret_value())


@extract_app.command("bank")
def extract_bank() -> None:
    """Bank transactions from the Plaid Sandbox Item (incremental /transactions/sync)."""
    settings = Settings()
    if settings.plaid_access_token is None:
        console.print("[red]No Plaid Item yet; run `recon seed plaid` first.[/red]")
        raise typer.Exit(1)
    wh = Warehouse(settings.duckdb_path)
    try:
        source = PlaidSource(_plaid_client(settings),
                             settings.plaid_access_token.get_secret_value())
        rows, removed, next_cursor = source.fetch(wh.state("plaid", "cursor"))
        rows += wh.tombstones("bank_transactions", removed)
        result = wh.load("plaid", {"bank_accounts": source.accounts(), "bank_transactions": rows},
                         state={"cursor": next_cursor})
    except ReconError as exc:
        console.print(f"[red]Extract failed:[/red] {exc}")
        raise typer.Exit(1) from exc
    finally:
        wh.close()
    n = result.rows_loaded
    console.print(f"bank_transactions: {n['bank_transactions']} new versions"
                  f" ({len(removed)} removed); bank_accounts: {n['bank_accounts']}")


def _run_matching(db_path: str) -> None:
    from collections import Counter

    from recon.matching.config import MatchConfig
    from recon.matching.engine import Matcher
    from recon.matching.payouts import match_payouts
    from recon.matching.store import load_cash, load_invoices, load_payouts, save, today

    cfg = MatchConfig.load()
    wh = Warehouse(db_path)
    try:
        decisions = Matcher(cfg, load_invoices(wh.con)).run(load_cash(wh.con))
        payouts, deposits = load_payouts(wh.con)
        decisions += match_payouts(payouts, deposits, cfg.payout_window_days, today())
        run_id = save(wh.con, decisions, cfg)
    finally:
        wh.close()
    tiers = Counter(d.tier for d in decisions if d.tier is not None)
    exceptions = Counter(d.exception for d in decisions if d.exception)
    console.print(f"run {run_id[:8]}: {len(decisions)} cash items; matched by tier "
                  f"{dict(sorted(tiers.items()))}; exceptions {dict(exceptions)}")


def _run_dbt(db_path: str) -> None:
    import os

    from dbt.cli.main import dbtRunner

    os.environ["RECON_DUCKDB_PATH"] = db_path
    result = dbtRunner().invoke(["build", "--project-dir", "dbt", "--profiles-dir", "dbt",
                                 "--quiet"])
    if not result.success:
        console.print("[red]dbt build failed[/red]")
        raise typer.Exit(1)


@app.command()
def match() -> None:
    """Run the matching engine on the current dbt models and store the decisions."""
    _run_matching(Settings().duckdb_path)


@app.command()
def evaluate(
    show_all: bool = typer.Option(False, "--all", help="List every invoice, not only misses."),
    min_precision: float = typer.Option(0.0, help="Exit 1 below this precision (CI gate)."),
    min_recall: float = typer.Option(0.0, help="Exit 1 below this recall (CI gate)."),
    db: str | None = typer.Option(None, help="DuckDB file (default: RECON_DUCKDB_PATH)."),
) -> None:
    """Compare the latest matching run with the seed's ground truth."""
    from recon.evaluate import metrics, outcomes

    wh = Warehouse(db or Settings().duckdb_path)
    try:
        results = outcomes(wh.con, Path("seeds/expected_matches.csv"))
    finally:
        wh.close()
    table = Table("invoice", "case", "expected", "got", "ok")
    for o in results:
        if show_all or not o.correct:
            exp = f"T{o.expected_tier or '-'} {o.expected_exception or ''}"
            got = f"T{o.tier or '-'} {', '.join(sorted(o.exceptions))}"
            table.add_row(o.invoice, o.case, exp, got, "✓" if o.correct else "✗")
    console.print(table)
    m = metrics(results)
    console.print(f"{m['correct']}/{m['invoices']} invoices correct · precision "
                  f"{m['precision']:.0%} · recall {m['recall']:.0%} · exception accuracy "
                  f"{m['exception_accuracy']:.0%}")
    if m["precision"] < min_precision or m["recall"] < min_recall:
        console.print(f"[red]Below gate: precision >= {min_precision:.0%}, "
                      f"recall >= {min_recall:.0%} required[/red]")
        raise typer.Exit(1)


@app.command()
def status() -> None:
    """Row counts per raw table and the latest extract runs."""
    wh = Warehouse(Settings().duckdb_path)
    try:
        counts = Table("raw table", "rows")
        for table, n in wh.row_counts().items():
            counts.add_row(table, str(n))
        console.print(counts)
        runs = Table("source", "started", "status", "rows", "watermark", "error")
        for source, started, state, rows, mark, error in wh.recent_runs():
            runs.add_row(source, f"{started:%Y-%m-%d %H:%M:%S}", state, str(rows or 0),
                         str(mark or ""), error or "")
        console.print(runs)
    finally:
        wh.close()


@seed_app.command("stripe")
def seed_stripe(
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the plan, create nothing."),
    seed: int = typer.Option(2026, help="RNG seed for the scenario plan."),
) -> None:
    """Pay the card cases of the November scenario plan on the Stripe test merchant."""
    from recon.seed import SEEDS_DIR, open_invoices, write_plan
    from recon.seed.plan import build_plan
    from recon.seed.stripe_seed import StripeSeeder

    settings = Settings()
    wh = Warehouse(settings.duckdb_path)
    try:
        invoices = open_invoices(wh)
    finally:
        wh.close()
    if not invoices:
        console.print("[red]No open invoices in raw.erp_invoices; run `recon extract erp`.[/red]")
        raise typer.Exit(1)
    plan = build_plan(invoices, seed)
    table = Table("case", "scenario", "channel", "invoices", "amount")
    for s in plan:
        table.add_row(s.case_id, s.case.value, s.channel.value, ", ".join(s.invoice_names),
                      f"${s.pay_cents / 100:,.2f}")
    console.print(table)
    if dry_run:
        return
    write_plan(plan)
    if settings.stripe_api_key is None or settings.stripe_account is None:
        console.print("[red]RECON_STRIPE_API_KEY and RECON_STRIPE_ACCOUNT must be set[/red]")
        raise typer.Exit(1)
    seeder = StripeSeeder(settings.stripe_api_key.get_secret_value(), settings.stripe_account,
                          Path(SEEDS_DIR) / "stripe_ledger.json")
    console.print(seeder.run(plan))


@seed_app.command("plaid")
def seed_plaid(
    payouts: bool = typer.Option(False, "--payouts",
                                 help="Also post bank deposits for Stripe payouts in raw."),
) -> None:
    """Post the transfer cases, noise lines and (optionally) Stripe payout deposits."""
    from datetime import date

    from recon.seed import SEEDS_DIR, load_plan
    from recon.seed.plaid_seed import PlaidSeeder, noise_lines, payout_lines, transfer_lines

    settings = Settings()
    seeder = PlaidSeeder(_plaid_client(settings), Path(SEEDS_DIR) / "plaid_ledger.json")
    token = settings.plaid_access_token.get_secret_value() if settings.plaid_access_token else None
    if token is None:
        token = seeder.create_item()
        with open(".env", "a") as fh:  # never printed
            fh.write(f"\nRECON_PLAID_ACCESS_TOKEN={token}\n")
        console.print("Created Plaid sandbox Item; access token saved to .env")

    start, last = date(2026, 11, 2), min(date.today(), date(2026, 11, 30))
    wh = Warehouse(settings.duckdb_path)
    try:
        invoice_dates = dict(wh.con.execute(
            "SELECT name, invoice_date FROM raw.erp_invoices WHERE invoice_date IS NOT NULL"
        ).fetchall())
        lines = transfer_lines(load_plan(), start, last, invoice_dates) + noise_lines(start)
        if payouts:
            rows = wh.con.execute(
                "SELECT id, amount, arrival_date::DATE, trace_id FROM raw.stripe_payouts"
                " QUALIFY row_number() OVER (PARTITION BY id ORDER BY _loaded_at DESC) = 1"
                " AND status = 'paid'").fetchall()
            lines += payout_lines([(r[0], r[1], r[2], r[3]) for r in rows])
    finally:
        wh.close()
    try:
        console.print(seeder.post(token, lines))
    except ReconError as exc:
        console.print(f"[red]Seed failed:[/red] {exc}")
        raise typer.Exit(1) from exc


snapshot_app = typer.Typer(help="Committed raw-layer snapshot (runs without sandbox keys).",
                           no_args_is_help=True)
app.add_typer(snapshot_app, name="snapshot")


@snapshot_app.command("export")
def snapshot_export() -> None:
    """Write all raw tables to seeds/snapshot/*.csv."""
    from recon.snapshot import export

    wh = Warehouse(Settings().duckdb_path)
    try:
        console.print(export(wh))
    finally:
        wh.close()


@app.command()
def demo(db: str = typer.Option("demo.duckdb", help="New DuckDB file to build.")) -> None:
    """Rebuild everything from the snapshot: load -> dbt -> match. No keys needed."""
    from recon.snapshot import load

    if Path(db).exists():
        Path(db).unlink()
    wh = Warehouse(db)
    try:
        console.print(load(wh))
    finally:
        wh.close()
    _run_dbt(db)
    _run_matching(db)
    _run_dbt(db)  # marts read the decisions the match just wrote
    console.print(f"Demo warehouse ready: {db}  (open it with RECON_DUCKDB_PATH={db} make app)")


if __name__ == "__main__":
    app()
