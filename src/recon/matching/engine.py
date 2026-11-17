"""Cash-to-invoice matching for the close period.

Three tiers, applied in order across ALL cash items, so a confident match is never
blocked by a weaker one that happened to be processed first:

  Tier 1  exact:      invoice reference (normalized) + amount to the cent
  Tier 2  fuzzy:      weighted score of amount (within tolerance), text (reference or
                      customer name) and date; accepted at >= threshold with a clear margin
  Tier 3  one-to-many: one transfer = a unique subset of one customer's open invoices

Every decision keeps its score breakdown so a reviewer (or auditor) sees *why*.
Cash that can't be placed goes to the exception queue with a reason code and, where
there is one, the best candidate as a suggestion.

Card-side facts that need a human even when the match itself is certain (refund,
dispute, duplicate, partial payment) are recorded as matched-with-exception.
"""

from __future__ import annotations

import itertools
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from recon.matching.config import MatchConfig
from recon.matching.text import name_score, ref_score


@dataclass(frozen=True)
class Invoice:
    number: str
    partner_id: int
    partner_name: str
    invoice_date: date
    due_date: date
    residual_cents: int


@dataclass(frozen=True)
class Cash:
    """A card charge or a bank receipt, normalized for matching."""
    source: str                  # "stripe_charge" | "bank_receipt"
    id: str
    day: date
    amount_cents: int            # what the customer paid
    net_cents: int               # what remains after refunds / disputes (cards)
    reference: str | None        # explicit invoice reference (Stripe metadata)
    text: str | None             # free text: description / bank memo
    payer: str | None            # billing name; for bank lines the memo carries it
    flags: frozenset[str] = frozenset()  # REFUNDED, PARTIAL_REFUND, DISPUTED


@dataclass
class Decision:
    source: str
    source_id: str
    tier: int | None
    score: float
    invoices: list[str]
    applied_cents: list[int]
    exception: str | None = None
    breakdown: dict[str, float] = field(default_factory=dict)
    rule: str = ""
    suggestion: list[str] = field(default_factory=list)
    writeoff_cents: int = 0


class Matcher:
    def __init__(self, cfg: MatchConfig, invoices: list[Invoice]) -> None:
        self.cfg = cfg
        self.invoices = {i.number: i for i in invoices}
        self.remaining = {i.number: i.residual_cents for i in invoices}

    # ------------------------------------------------------------ scoring
    def _tolerance(self, cash: Cash) -> int:
        return self.cfg.tolerance_stripe if cash.source == "stripe_charge" \
            else self.cfg.tolerance_transfer

    def _amount_score(self, cash: Cash, inv: Invoice) -> float:
        # Against what is still open in this run, not the ERP's original residual:
        # an invoice settled a moment ago by an exact match must not attract more cash.
        diff = abs(cash.amount_cents - self.remaining[inv.number])
        if diff == 0:
            return 1.0
        return 0.9 if diff <= self._tolerance(cash) else 0.0

    def _date_score(self, day: date, inv: Invoice) -> float:
        w = timedelta(days=self.cfg.date_window_days)
        if inv.invoice_date - w <= day <= inv.due_date + w:
            return 1.0
        if day < inv.invoice_date - w:
            return 0.0  # paid before the invoice existed
        late = (day - inv.due_date - w).days
        return max(0.5, 1.0 - 0.5 * late / self.cfg.late_decay_days)

    def _text_score(self, cash: Cash, inv: Invoice) -> tuple[float, float]:
        ref = max(ref_score(inv.number, cash.reference), ref_score(inv.number, cash.text))
        name = max(name_score(inv.partner_name, cash.payer),
                   name_score(inv.partner_name, cash.text))
        return ref, name

    def score(self, cash: Cash, inv: Invoice) -> dict[str, float]:
        ref, name = self._text_score(cash, inv)
        amount = self._amount_score(cash, inv)
        text = max(ref, 0.95 * name)  # a full customer name is almost as good as a ref
        date_ = self._date_score(cash.day, inv)
        total = (self.cfg.w_amount * amount + self.cfg.w_text * text + self.cfg.w_date * date_)
        return {"amount": amount, "ref": ref, "name": name, "text": text, "date": date_,
                "total": round(total, 4)}

    # ------------------------------------------------------------ tiers
    def _tier1(self, cash: Cash) -> Decision | None:
        """Exact reference. Amount must match the residual, except on the card side,
        where a cited invoice is certain and the amount difference is the finding."""
        hits = [inv for inv in self.invoices.values()
                if ref_score(inv.number, cash.reference or cash.text) == 1.0
                and (cash.reference or len(self._cited(cash)) == 1)]
        if len(hits) != 1:
            return None
        inv = hits[0]
        exact = cash.amount_cents == inv.residual_cents
        if not exact and cash.source != "stripe_charge":
            return None
        return self._apply(cash, inv, tier=1, score=1.0, rule="ref+amount" if exact else "ref",
                           breakdown=self.score(cash, inv))

    def _cited(self, cash: Cash) -> list[str]:
        return [n for n in self.invoices if ref_score(n, cash.text) == 1.0]

    def _tier2(self, cash: Cash) -> Decision | None:
        open_invoices = [i for i in self.invoices.values() if self.remaining[i.number] > 0]
        ranked = sorted(((self.score(cash, inv), inv) for inv in open_invoices),
                        key=lambda x: x[0]["total"], reverse=True)
        ranked = [r for r in ranked if r[0]["amount"] > 0]
        if not ranked:
            return None
        best, inv = ranked[0]
        if best["total"] < self.cfg.accept_threshold:
            return None
        cited = [n for n in self._cited(cash) if self.remaining.get(n, 0) > 0]
        if cited and inv.number not in cited:
            # The amount fits one invoice but the customer named another open one.
            # Which one they meant is a question for a human, not for the score.
            return Decision(cash.source, cash.id, None, best["total"], [], [],
                            exception="REFERENCE_CONFLICT", breakdown=best, rule="fuzzy",
                            suggestion=[*cited, inv.number])
        if best["ref"] < 1.0:
            # Without an exact reference, candidates the amount and the text can't tell
            # apart are a tie. The date ranks the suggestions but never decides: customers
            # do pay before the invoice date.
            ties = [i for sc, i in ranked[1:]
                    if sc["amount"] == best["amount"]
                    and abs(sc["text"] - best["text"]) < self.cfg.ambiguity_margin]
            if ties:
                return Decision(cash.source, cash.id, None, best["total"], [], [],
                                exception="AMBIGUOUS", breakdown=best, rule="fuzzy",
                                suggestion=[inv.number, *(i.number for i in ties)])
        if len(ranked) > 1 and best["total"] - ranked[1][0]["total"] < self.cfg.ambiguity_margin:
            return Decision(cash.source, cash.id, None, best["total"], [], [],
                            exception="AMBIGUOUS", breakdown=best, rule="fuzzy",
                            suggestion=[inv.number, ranked[1][1].number])
        return self._apply(cash, inv, tier=2, score=best["total"], rule="fuzzy", breakdown=best)

    def _tier3(self, cash: Cash) -> Decision | None:
        if cash.source != "bank_receipt":
            return None
        by_partner: dict[int, list[Invoice]] = defaultdict(list)
        for inv in self.invoices.values():
            if self.remaining[inv.number] > 0 and \
                    (cash.day - inv.invoice_date).days <= self.cfg.t3_lookback_days:
                by_partner[inv.partner_id].append(inv)
        for invs in by_partner.values():
            if name_score(invs[0].partner_name, cash.text) < self.cfg.t3_min_name_score:
                continue
            limited = Decision(cash.source, cash.id, None, 0.0, [], [],
                               exception="SEARCH_LIMIT", rule="subset-sum",
                               breakdown={"open_invoices": float(len(invs))})
            if len(invs) > self.cfg.t3_max_candidates:
                return limited  # never search a truncated list: "unique" could be wrong
            subsets: list[tuple[Invoice, ...]] = []
            tried = 0
            for k in range(2, min(self.cfg.t3_max_invoices, len(invs)) + 1):
                for combo in itertools.combinations(invs, k):
                    tried += 1
                    if tried > self.cfg.t3_max_combinations:
                        return limited
                    if abs(sum(self.remaining[i.number] for i in combo) - cash.amount_cents) \
                            <= self.cfg.tolerance_transfer:
                        subsets.append(combo)
                        if len(subsets) > 1:
                            break  # two fits already make it ambiguous
                if len(subsets) > 1:
                    break
            if len(subsets) != 1:
                if subsets:
                    return Decision(cash.source, cash.id, None, 0.0, [], [],
                                    exception="AMBIGUOUS", rule="subset-sum",
                                    suggestion=[i.number for i in subsets[0]])
                continue
            combo = subsets[0]
            cites_all = all(ref_score(i.number, cash.text) == 1.0 for i in combo)
            score = self.cfg.t3_score_with_refs if cites_all else self.cfg.t3_score_unique
            names = [i.number for i in combo]
            if score < self.cfg.accept_threshold:
                return Decision(cash.source, cash.id, None, score, [], [],
                                exception="LOW_CONFIDENCE", rule="subset-sum", suggestion=names)
            applied = [self.remaining[n] for n in names]
            for n in names:
                self.remaining[n] = 0
            return Decision(cash.source, cash.id, 3, score, names, applied, rule="subset-sum",
                            breakdown={"invoices": float(len(names)),
                                       "cites_all_refs": float(cites_all)})
        return None

    # ------------------------------------------------------------ apply
    def _apply(self, cash: Cash, inv: Invoice, *, tier: int, score: float, rule: str,
               breakdown: dict[str, float]) -> Decision:
        open_cents = self.remaining[inv.number]
        d = Decision(cash.source, cash.id, tier, score, [inv.number], [0],
                     breakdown=breakdown, rule=rule)
        if open_cents <= 0:
            d.exception = "DUPLICATE_PAYMENT"
            return d
        for flag in ("REFUNDED", "DISPUTED", "PARTIAL_REFUND"):
            if flag in cash.flags:
                d.exception = flag
                break
        applied = min(cash.net_cents, open_cents)
        if d.exception is None and cash.amount_cents > open_cents:
            # Apply what is open; the excess is customer money we owe or must place.
            # It must never vanish into clearing silently.
            d.exception = "OVERPAYMENT"
        elif d.exception is None and cash.amount_cents < open_cents:
            gap = open_cents - cash.amount_cents
            if cash.source == "bank_receipt" and gap <= self.cfg.tolerance_transfer:
                d.writeoff_cents = gap          # small bank fee: apply and write off
                applied = open_cents - gap
            else:
                d.exception = "PARTIAL_PAYMENT"
        self.remaining[inv.number] = open_cents - applied - d.writeoff_cents
        d.applied_cents = [max(0, applied)]
        return d

    def _unplaced(self, cash: Cash) -> Decision:
        """No acceptable match: say why, and point at the most likely invoice."""
        ranked = sorted(((self.score(cash, inv), inv) for inv in self.invoices.values()),
                        key=lambda x: (x[0]["ref"], x[0]["total"]), reverse=True)
        best, inv = ranked[0] if ranked else ({}, None)
        if inv is not None and self.remaining[inv.number] <= 0 and (
                best.get("ref") == 1.0 or best.get("name", 0) >= 0.95) \
                and cash.amount_cents == inv.residual_cents:
            # Same amount, same customer/reference, invoice already settled in this run.
            return Decision(cash.source, cash.id, None, best["total"], [], [],
                            exception="DUPLICATE_PAYMENT", breakdown=best, rule="settled",
                            suggestion=[inv.number])
        if inv is not None and best.get("ref") == 1.0:
            code = "OVERPAYMENT" if cash.amount_cents > inv.residual_cents else "AMOUNT_MISMATCH"
            return Decision(cash.source, cash.id, None, best["total"], [], [], exception=code,
                            breakdown=best, rule="ref", suggestion=[inv.number])
        if cash.source == "bank_receipt":
            return Decision(cash.source, cash.id, None, best.get("total", 0.0), [], [],
                            exception="UNAPPLIED_CASH", breakdown=best,
                            suggestion=[inv.number] if inv and best["total"] >= 0.6 else [])
        return Decision(cash.source, cash.id, None, best.get("total", 0.0), [], [],
                        exception="NO_CANDIDATE", breakdown=best,
                        suggestion=[inv.number] if inv and best["total"] >= 0.6 else [])

    def run(self, cash_items: list[Cash]) -> list[Decision]:
        decided: dict[str, Decision] = {}
        ordered = sorted(cash_items, key=lambda c: (c.day, c.id))
        for tier in (self._tier1, self._tier2, self._tier3):
            for cash in ordered:
                if cash.id in decided:
                    continue
                d = tier(cash)
                if d is not None:
                    decided[cash.id] = d
        return [decided.get(c.id) or self._unplaced(c) for c in ordered]
