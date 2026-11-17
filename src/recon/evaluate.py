"""Score the matching engine against the seed's ground truth (seeds/expected_matches.csv).

Unit of evaluation: one invoice of the scenario plan. For each, the engine's outcome is
(tier of the link it chose, exception codes raised about that invoice). An outcome is
correct when both agree with the plan.

- Precision (auto-match): of the invoices the engine linked, how many should be linked
  at that tier. A wrong automatic match is the expensive error in cash application.
- Recall (auto-match): of the invoices the plan says are matchable, how many the
  engine linked at the expected tier.
- Exception accuracy: of the invoices that should raise a code, how many got exactly it.
"""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import duckdb


@dataclass
class InvoiceOutcome:
    invoice: str
    case: str
    expected_tier: int | None
    expected_exception: str | None
    tier: int | None
    exceptions: set[str]

    @property
    def correct(self) -> bool:
        exc_ok = (self.expected_exception in self.exceptions) if self.expected_exception \
            else not self.exceptions
        return self.tier == self.expected_tier and exc_ok


def outcomes(con: duckdb.DuckDBPyConnection, expected_csv: Path) -> list[InvoiceOutcome]:
    tiers: dict[str, int] = {}
    excs: dict[str, set[str]] = defaultdict(set)
    rows = con.execute("""
        SELECT invoice_number, tier, exception_code, suggestion
        FROM engine.current_decisions WHERE source <> 'stripe_payout'
    """).fetchall()
    for inv, tier, exc, suggestion in rows:
        if inv is not None and tier is not None:
            tiers[inv] = min(tier, tiers.get(inv, tier))
        targets = [inv] if inv else json.loads(suggestion or "[]")
        if exc:
            for t in targets:
                excs[t].add(exc)
    result = []
    with expected_csv.open() as fh:
        for r in csv.DictReader(fh):
            result.append(InvoiceOutcome(
                r["invoice"], r["case"],
                int(r["expected_tier"]) if r["expected_tier"] else None,
                r["expected_exception"] or None,
                tiers.get(r["invoice"]), excs.get(r["invoice"], set())))
    return result


def metrics(results: list[InvoiceOutcome]) -> dict[str, float]:
    linked = [o for o in results if o.tier is not None]
    should = [o for o in results if o.expected_tier is not None]
    with_exc = [o for o in results if o.expected_exception]
    def share(hits: int, n: int) -> float:
        return hits / n if n else 1.0

    return {
        "invoices": len(results),
        "correct": sum(o.correct for o in results),
        "precision": share(sum(o.tier == o.expected_tier for o in linked), len(linked)),
        "recall": share(sum(o.tier == o.expected_tier for o in should), len(should)),
        "exception_accuracy": (sum(o.expected_exception in o.exceptions for o in with_exc)
                               / len(with_exc)) if with_exc else 1.0,
    }
