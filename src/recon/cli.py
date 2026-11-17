"""Command-line entry point: `recon extract erp`, `recon status`."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from recon.config import Settings
from recon.errors import ReconError
from recon.sources.odoo import OdooInvoiceSource, XmlRpcOdoo
from recon.sources.stripe_source import StripeClientAPI, StripeSource
from recon.warehouse import Warehouse

app = typer.Typer(help="Cash reconciliation engine (Stripe / bank / ERP).", no_args_is_help=True)
extract_app = typer.Typer(help="Pull source data into raw DuckDB tables.", no_args_is_help=True)
app.add_typer(extract_app, name="extract")
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
