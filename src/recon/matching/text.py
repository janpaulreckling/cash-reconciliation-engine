"""Turning what customers type into something comparable."""

from __future__ import annotations

import re

from rapidfuzz import fuzz

LEGAL_SUFFIXES = re.compile(r"\b(inc|llc|ltd|co|corp|company|gmbh)\b\.?", re.I)
INVOICE_RE = re.compile(r"(?i)\b(?:inv(?:oice)?)[\s/#:-]*(20\d{2})?[\s/-]*0*(\d{1,6})\b")


def compact(text: str) -> str:
    """Letters and digits only, upper case: 'inv-2026-00085' -> 'INV202600085'."""
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def invoice_key(invoice_number: str) -> tuple[int, int] | None:
    """INV/2026/00087 -> (2026, 87)."""
    m = re.fullmatch(r"[A-Z]+/(\d{4})/(\d+)", invoice_number)
    return (int(m.group(1)), int(m.group(2))) if m else None


def refs_in(memo: str) -> set[tuple[int | None, int]]:
    """Invoice references found in free text, tolerant of spacing, prefixes, zeros.
    'INV 2026 82' -> {(2026, 82)}, 'Invoice 87' -> {(None, 87)}."""
    found: set[tuple[int | None, int]] = set()
    for year, num in INVOICE_RE.findall(memo or ""):
        found.add((int(year) if year else None, int(num)))
    return found


def ref_score(invoice_number: str, memo: str | None) -> float:
    """1.0 if the memo cites this invoice exactly (after normalization), otherwise a
    fuzzy similarity of the compacted strings (catches dropped/transposed digits)."""
    if not memo:
        return 0.0
    key = invoice_key(invoice_number)
    if key:
        for year, num in refs_in(memo):
            if num == key[1] and year in (None, key[0]):
                return 1.0
    target = compact(invoice_number)
    best = 0.0
    for token in re.findall(r"[A-Za-z]*[\s/-]*\d[\d\s/-]*", memo):
        best = max(best, fuzz.ratio(target, compact(token)) / 100)
    return best if best >= 0.8 else 0.0


def normalize_name(name: str) -> str:
    return re.sub(r"\s+", " ", LEGAL_SUFFIXES.sub("", name.lower())).strip(" .,")


def name_score(partner_name: str, text: str | None) -> float:
    if not text:
        return 0.0
    return fuzz.token_set_ratio(normalize_name(partner_name), normalize_name(text)) / 100
