"""Score the matching engine against the seed's ground truth.

Ground truth comes in two files written by the seed:
- seeds/expected_matches.csv: per invoice, the expected tier and exception code
- seeds/expected_cash.csv:    per scenario, the real cash item(s) created for it

A link counts only as a (cash item, invoice) PAIR. Linking the right invoice with the
wrong customer's money is the expensive error in cash application, so it must show up
as a precision miss, not hide behind a correct-looking invoice.

- Precision: of the pairs the engine linked automatically, how many are true pairs.
- Recall:    of the invoices the plan says are matchable, how many were linked to their
             own cash at the expected tier.
- Exception accuracy: of the scenarios that should raise a code, how many raised exactly
             it on their own cash item, pointing at their own invoice ("REVIEW" = any
             code, i.e. it must reach a human instead of being auto-applied).
"""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import duckdb


@dataclass
class InvoiceOutcome:
    invoice: str
    case_id: str
    case: str
    expected_tier: int | None
    expected_exception: str | None
    tier: int | None = None
    exceptions: set[str] = field(default_factory=set)
    linked_to_own_cash: bool = False

    @property
    def correct(self) -> bool:
        tier_ok = self.tier == self.expected_tier and (
            self.expected_tier is None or self.linked_to_own_cash)
        return tier_ok and self.exception_ok

    @property
    def exception_ok(self) -> bool:
        if self.expected_exception == "REVIEW":  # any code, as long as a human sees it
            return bool(self.exceptions - {"WRONG_CASH_LINKED"})
        if self.expected_exception:
            return self.expected_exception in self.exceptions
        return not self.exceptions


@dataclass
class Evaluation:
    outcomes: list[InvoiceOutcome]
    engine_pairs: set[tuple[str, str]]
    true_pairs: set[tuple[str, str]]


def evaluate(con: duckdb.DuckDBPyConnection, expected_csv: Path,
             cash_csv: Path) -> Evaluation:
    with expected_csv.open() as fh:
        expected = list(csv.DictReader(fh))
    with cash_csv.open() as fh:
        cash_of: dict[str, set[str]] = defaultdict(set)
        for r in csv.DictReader(fh):
            cash_of[r["case_id"]].add(r["source_id"])

    true_pairs = {(sid, r["invoice"]) for r in expected for sid in cash_of[r["case_id"]]}
    decisions = con.execute("""
        SELECT source_id, invoice_number, tier, exception_code, suggestion
        FROM engine.current_decisions WHERE source <> 'stripe_payout'
    """).fetchall()
    engine_pairs: set[tuple[str, str]] = set()
    pair_tier: dict[tuple[str, str], int] = {}
    exc_on: dict[tuple[str, str], set[str]] = defaultdict(set)
    for sid, inv, tier, exc, suggestion in decisions:
        if inv is not None and tier is not None:
            engine_pairs.add((sid, inv))
            pair_tier[(sid, inv)] = tier
        if exc:
            for target in ([inv] if inv else json.loads(suggestion or "[]")):
                exc_on[(sid, target)].add(exc)

    outcomes = []
    for r in expected:
        o = InvoiceOutcome(r["invoice"], r["case_id"], r["case"],
                           int(r["expected_tier"]) if r["expected_tier"] else None,
                           r["expected_exception"] or None)
        own = [(sid, o.invoice) for sid in cash_of[o.case_id]]
        tiers = [pair_tier[p] for p in own if p in pair_tier]
        o.tier = min(tiers) if tiers else None
        o.linked_to_own_cash = bool(tiers)
        for p in own:
            o.exceptions |= exc_on.get(p, set())
        # Linked to someone else's cash? That is a wrong match on this invoice.
        if any(inv == o.invoice and (sid, inv) not in true_pairs for sid, inv in engine_pairs):
            o.exceptions.add("WRONG_CASH_LINKED")
        outcomes.append(o)
    return Evaluation(outcomes, engine_pairs, true_pairs)


def metrics(ev: Evaluation) -> dict[str, float]:
    def share(hits: int, n: int) -> float:
        return hits / n if n else 1.0

    should = [o for o in ev.outcomes if o.expected_tier is not None]
    with_exc = [o for o in ev.outcomes if o.expected_exception]
    return {
        "invoices": len(ev.outcomes),
        "correct": sum(o.correct for o in ev.outcomes),
        "linked_pairs": len(ev.engine_pairs),
        "precision": share(len(ev.engine_pairs & ev.true_pairs), len(ev.engine_pairs)),
        "recall": share(sum(o.linked_to_own_cash and o.tier == o.expected_tier
                            for o in should), len(should)),
        "exception_accuracy": share(sum(o.exception_ok for o in with_exc), len(with_exc)),
        "wrong_auto_links": len(ev.engine_pairs - ev.true_pairs),
    }
