from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class MatchConfig:
    accept_threshold: float
    ambiguity_margin: float
    tolerance_stripe: int
    tolerance_transfer: int
    w_amount: float
    w_text: float
    w_date: float
    date_window_days: int
    late_decay_days: int
    t3_max_invoices: int
    t3_lookback_days: int
    t3_min_name_score: float
    t3_score_with_refs: float
    t3_score_unique: float
    payout_window_days: int

    @classmethod
    def load(cls, path: Path = Path("config/matching.yaml")) -> MatchConfig:
        c: dict[str, Any] = yaml.safe_load(path.read_text())
        return cls(
            accept_threshold=c["accept_threshold"],
            ambiguity_margin=c["ambiguity_margin"],
            tolerance_stripe=c["amount_tolerance_cents"]["stripe"],
            tolerance_transfer=c["amount_tolerance_cents"]["transfer"],
            w_amount=c["weights"]["amount"],
            w_text=c["weights"]["text"],
            w_date=c["weights"]["date"],
            date_window_days=c["date_window_days"],
            late_decay_days=c["late_decay_days"],
            t3_max_invoices=c["tier3"]["max_invoices"],
            t3_lookback_days=c["tier3"]["lookback_days"],
            t3_min_name_score=c["tier3"]["min_name_score"],
            t3_score_with_refs=c["tier3"]["score_unique_with_refs"],
            t3_score_unique=c["tier3"]["score_unique"],
            payout_window_days=c["payouts"]["arrival_window_days"],
        )
