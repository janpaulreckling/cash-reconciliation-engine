"""Design tokens and small HTML components for the review app.

Colors are roles, not raw hex, and derive from the active theme's text color, so every
component reads correctly in Streamlit's light and dark theme. Status colors are
fixed across themes and always ship with an icon and a label, never color alone.
"""

from __future__ import annotations

from datetime import date
from html import escape
from typing import Any

# Roles derived from the theme's own text color (currentColor), so the same CSS is
# right in Streamlit's light and dark theme without knowing which one is active.
ROLE_VARS = (
    "--text-2: color-mix(in srgb, currentColor 72%, transparent);"
    "--text-3: color-mix(in srgb, currentColor 55%, transparent);"
    "--raised: color-mix(in srgb, currentColor 3.5%, transparent);"
    "--sunken: color-mix(in srgb, currentColor 6%, transparent);"
    "--border: color-mix(in srgb, currentColor 13%, transparent);"
    "--track: color-mix(in srgb, currentColor 11%, transparent);"
    "--accent: #3987e5;"
    "--accent-soft: color-mix(in srgb, #3987e5 16%, transparent);"
)
STATUS = {"good": "#0ca30c", "warning": "#fab219", "serious": "#ec835a", "critical": "#d03b3b"}

# Exception codes in plain language: what happened, what the reviewer should do.
CODES: dict[str, tuple[str, str, str, str]] = {
    # code: (label, status, what happened, recommended action)
    "DISPUTED": ("Disputed", "critical",
                 "The cardholder disputed this payment. Stripe withdrew the amount plus a "
                 "$15 fee until the dispute is decided.",
                 "Submit evidence in Stripe; keep the invoice open until the outcome."),
    "PAYOUT_SUM_BREAK": ("Payout doesn't add up", "critical",
                         "The payout differs from the charges, fees and refunds Stripe "
                         "lists inside it.", "Open a Stripe support case before closing."),
    "PAYOUT_NOT_IN_BANK": ("Payout not in bank", "critical",
                           "Stripe paid out, but no matching deposit reached the bank.",
                           "Check the bank account on file and the trace ID."),
    "DUPLICATE_PAYMENT": ("Duplicate payment", "serious",
                          "The customer paid an invoice that was already settled.",
                          "Refund the customer or hold it as a credit on account."),
    "OVERPAYMENT": ("Overpayment", "serious",
                    "The payment cites an invoice but exceeds what was open on it.",
                    "Apply the invoice amount, hold the rest as customer credit."),
    "AMOUNT_MISMATCH": ("Short payment", "serious",
                        "The payment cites an invoice but is short by more than the "
                        "$1.00 bank-fee tolerance.",
                        "Ask the customer about the difference, or apply and dun the rest."),
    "UNAPPLIED_CASH": ("Unidentified deposit", "serious",
                       "Money came in that no open invoice explains.",
                       "Identify the payer; until then it stays in Unapplied Cash."),
    "NO_CANDIDATE": ("Unknown card payment", "serious",
                     "A card payment references no open invoice.",
                     "Find the order it belongs to, or refund it."),
    "AMBIGUOUS": ("Ambiguous", "warning",
                  "Two or more invoices fit equally well, so nothing was applied.",
                  "Pick the right invoice from the candidates."),
    "LOW_CONFIDENCE": ("Low confidence", "warning",
                       "A likely match, but below the auto-apply threshold.",
                       "Confirm the suggested invoice(s)."),
    "PARTIAL_PAYMENT": ("Partial payment", "warning",
                        "Matched with certainty, but the payment covers only part of the "
                        "invoice.", "Apply it; the rest stays open for dunning."),
    "PARTIAL_REFUND": ("Partially refunded", "warning",
                       "Matched with certainty; part of the payment was refunded.",
                       "Apply the net amount; issue a credit note for the refund."),
    "REFUNDED": ("Refunded", "warning",
                 "Matched with certainty, then fully refunded. No cash was applied.",
                 "Confirm the refund was intended; the invoice stays open."),
    "PAYOUT_NO_COMPONENTS": ("Manual payout", "warning",
                             "Stripe doesn't list the contents of manual payouts.",
                             "Reconcile it against the Stripe balance report."),
    "DEPOSIT_WITHOUT_PAYOUT": ("Unexpected Stripe deposit", "serious",
                               "A Stripe deposit arrived that matches no payout.",
                               "Check for a payout from another Stripe account."),
}
ICONS = {"good": "✓", "warning": "!", "serious": "▲", "critical": "■"}
TIER_LABEL = {1: "Exact", 2: "Fuzzy", 3: "One-to-many"}


def money(cents: int | None, signed: bool = False) -> str:
    if cents is None:
        return "–"
    sign = "−" if cents < 0 else ("+" if signed and cents > 0 else "")
    return f"{sign}${abs(cents) / 100:,.2f}"


def short_date(d: date | None) -> str:
    return d.strftime("%b %-d") if d else "–"


def css() -> str:
    vars_ = ROLE_VARS + "".join(f"--st-{k}:{v};" for k, v in STATUS.items())
    return f"""<style>
.stApp {{ {vars_} }}
.block-container {{ padding-top: 2.2rem; max-width: 1360px; }}
h1, h2, h3 {{ letter-spacing: -0.01em; }}
.num {{ font-variant-numeric: tabular-nums; }}
.eyebrow {{ color: var(--text-3); font-size: .78rem; letter-spacing: .06em;
            text-transform: uppercase; font-weight: 600; }}
.title {{ font-size: 1.65rem; font-weight: 650; margin: .1rem 0 .2rem; }}
.subtitle {{ color: var(--text-2); font-size: .92rem; }}
.tiles {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
          gap: 12px; margin: 1.1rem 0 1.4rem; }}
.tile {{ background: var(--raised); border: 1px solid var(--border); border-radius: 12px;
         padding: 14px 16px 12px; }}
.tile .label {{ color: var(--text-2); font-size: .8rem; font-weight: 550; }}
.tile .value {{ color: inherit; font-size: 1.55rem; font-weight: 650; margin-top: 2px;
                font-variant-numeric: tabular-nums; letter-spacing: -0.01em; }}
.tile .note {{ color: var(--text-3); font-size: .76rem; margin-top: 2px; }}
.tile .note.ok {{ color: var(--text-2); }}
.tile .note.ok::before {{ content: "✓ "; color: var(--st-good); font-weight: 700; }}
.progress {{ height: 6px; background: var(--track); border-radius: 99px; margin-top: 9px;
             overflow: hidden; }}
.progress > span {{ display: block; height: 100%; background: var(--accent);
                    border-radius: 99px; }}
.chip {{ display: inline-flex; align-items: center; gap: 6px; padding: 2px 9px 2px 7px;
         border-radius: 99px; font-size: .76rem; font-weight: 600; color: inherit;
         background: var(--sunken); border: 1px solid var(--border); white-space: nowrap; }}
.chip .dot {{ width: 15px; height: 15px; border-radius: 99px; display: inline-grid;
              place-items: center; font-size: .6rem; color: #fff; font-weight: 800; }}
.chip.tier {{ background: var(--accent-soft); border-color: transparent; }}
.card {{ background: var(--raised); border: 1px solid var(--border); border-radius: 12px;
         padding: 16px 18px; margin-bottom: 12px; }}
.card h4 {{ margin: 0 0 10px; font-size: .78rem; color: var(--text-3); font-weight: 650;
            letter-spacing: .05em; text-transform: uppercase; }}
.kv {{ display: grid; grid-template-columns: 118px 1fr; row-gap: 6px; column-gap: 12px;
       font-size: .9rem; }}
.kv .k {{ color: var(--text-2); }}
.kv .v {{ color: inherit; font-variant-numeric: tabular-nums; word-break: break-word; }}
.explain {{ border-left: 3px solid var(--accent); padding: 8px 14px; margin: 4px 0 14px;
            background: var(--sunken); border-radius: 0 10px 10px 0; font-size: .9rem;
            color: inherit; }}
.explain b {{ color: inherit; }}
.explain .act {{ color: var(--text-2); margin-top: 4px; }}
.meter {{ display: grid; grid-template-columns: 92px 1fr 46px; align-items: center;
          gap: 10px; font-size: .85rem; margin: 6px 0; }}
.meter .k {{ color: var(--text-2); }}
.meter .bar {{ height: 6px; background: var(--track); border-radius: 99px; overflow: hidden; }}
.meter .bar > span {{ display: block; height: 100%; background: var(--accent);
                      border-radius: 99px; }}
.meter .v {{ text-align: right; color: inherit; font-variant-numeric: tabular-nums; }}
.diff {{ font-variant-numeric: tabular-nums; font-weight: 600; }}
.reviewed {{ font-size: .85rem; color: var(--text-2); margin-top: 6px; }}
.reviewed::before {{ content: "✓ "; color: var(--st-good); font-weight: 700; }}
.empty {{ border: 1px dashed var(--border); border-radius: 12px; padding: 28px;
          text-align: center; color: var(--text-2); background: var(--raised); }}
.foot {{ color: var(--text-3); font-size: .78rem; margin-top: 2rem; }}
div[data-testid="stTabs"] button p {{ font-weight: 600; }}
</style>"""


def tile(label: str, value: str, note: str = "", ok: bool = False,
         progress: float | None = None) -> str:
    bar = (f'<div class="progress"><span style="width:{max(0, min(1, progress)) * 100:.1f}%">'
           "</span></div>") if progress is not None else ""
    cls = "note ok" if ok else "note"
    return (f'<div class="tile"><div class="label">{escape(label)}</div>'
            f'<div class="value">{escape(value)}</div>'
            f'<div class="{cls}">{escape(note)}</div>{bar}</div>')


def code_chip(code: str | None) -> str:
    if not code:
        return ""
    label, status, *_ = CODES.get(code, (code, "warning", "", ""))
    return (f'<span class="chip"><span class="dot" style="background:var(--st-{status})">'
            f"{ICONS[status]}</span>{escape(label)}</span>")


def tier_chip(tier: int | None) -> str:
    if tier is None:
        return ""
    return f'<span class="chip tier">Tier {tier} · {TIER_LABEL.get(tier, "")}</span>'


def kv_card(title: str, rows: list[tuple[str, str]], extra: str = "") -> str:
    body = "".join(f'<div class="k">{escape(k)}</div><div class="v">{v}</div>'
                   for k, v in rows)
    return f'<div class="card"><h4>{escape(title)}</h4><div class="kv">{body}</div>{extra}</div>'


def meters(breakdown: dict[str, Any]) -> str:
    parts = [("Amount", "amount"), ("Reference", "ref"), ("Customer", "name"), ("Date", "date")]
    out = []
    for label, key in parts:
        if key not in breakdown:
            continue
        v = float(breakdown[key])
        out.append(f'<div class="meter"><span class="k">{label}</span>'
                   f'<span class="bar"><span style="width:{v * 100:.0f}%"></span></span>'
                   f'<span class="v">{v:.2f}</span></div>')
    if "total" in breakdown:
        out.append(f'<div class="meter"><span class="k"><b>Score</b></span><span></span>'
                   f'<span class="v"><b>{float(breakdown["total"]):.2f}</b></span></div>')
    return "".join(out)
