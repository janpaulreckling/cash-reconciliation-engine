"""Cash reconciliation review app: close status, exception queue, evidence, decisions.

Run locally:  uv run --group app streamlit run src/recon/app/streamlit_app.py
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import streamlit as st

from recon.app import data
from recon.app.ui import (
    CODES,
    ICONS,
    TIER_LABEL,
    code_chip,
    css,
    kv_card,
    meters,
    money,
    short_date,
    tier_chip,
    tile,
)

st.set_page_config(page_title="Cash Reconciliation · Northwind", page_icon="◧", layout="wide")
st.html(css())

@st.cache_resource(show_spinner="Building the demo warehouse from the committed snapshot…")
def _bootstrap() -> bool:
    data.ensure_demo_db()
    return True


_bootstrap()
status = data.close_status()
if not status:
    st.markdown('<div class="empty">No matching run yet. Run <code>make pipeline</code> '
                "first.</div>", unsafe_allow_html=True)
    st.stop()

exceptions = data.exceptions()
open_items = [e for e in exceptions if not e["review_action"]]

# ------------------------------------------------------------------ header
period = status["period_start"].strftime("%B %Y")
st.html(f"""
<div class="eyebrow">Northwind Outfitters · Cash reconciliation</div>
<div class="title">{period} close</div>
<div class="lede">At month-end, this checks that every customer payment in Stripe and the
bank belongs to an invoice, and shows Accounting only the ones that don't add up.</div>
<div class="subtitle">Matched {status['matched_at']:%b %-d, %H:%M} ·
{status['cash_items']} payments against {status['open_invoices']} open invoices</div>
""")

rate = float(status["auto_match_rate"] or 0)
reviewed = len(exceptions) - len(open_items)
st.html('<div class="tiles">' + "".join([
    tile("Auto-matched", f"{rate:.0%}",
         f"{status['auto_matched_items']} of {status['cash_items']} cash items", progress=rate),
    tile("Cash received", money(status["cash_cents"]), "card + bank, this period"),
    tile("Needs review", money(status["cash_needing_review_cents"]),
         f"{len(open_items)} open · {reviewed} decided",
         progress=reviewed / len(exceptions) if exceptions else 1.0),
    tile("Applied to invoices", money(status["cash_applied_cents"]),
         f"of {money(status['open_ar_cents'])} open AR"),
    tile("Stripe clearing", money(status["stripe_clearing_cents"]),
         "equals Stripe balance not yet paid out", ok=True),
    tile("Unapplied cash", money(status["unapplied_cash_cents"]),
         "parked until identified"),
]) + "</div>")

tab_queue, tab_matched, tab_journal, tab_payouts = st.tabs(
    [f"Review queue ({len(open_items)})", "Matched cash", "Journal suggestions", "Payouts"])

# ------------------------------------------------------------------ queue
with tab_queue:
    left, right = st.columns([1.05, 1], gap="large")
    with left:
        show_done = st.toggle("Show decided items", value=False)
        rows = exceptions if show_done else open_items
        if not rows:
            st.html('<div class="empty">All exceptions decided. The close can move to '
                    "journal review.</div>")
        else:
            table = [{
                "Priority": {1: "■ Money at risk", 2: "▲ Review", 3: "! Confirm"}[r["priority"]],
                "Issue": CODES.get(r["exception_code"], (r["exception_code"],))[0],
                "Amount": r["amount_cents"] / 100,
                "Date": r["cash_date"],
                "Source": "Card" if r["source"] == "stripe_charge" else "Bank",
                "Detail": (r["cash_text"] or "").strip()[:60],
                "Decision": (r["review_action"] or "").capitalize(),
            } for r in rows]
            event = st.dataframe(
                table, hide_index=True, use_container_width=True, height=420,
                on_select="rerun", selection_mode="single-row",
                column_config={
                    "Amount": st.column_config.NumberColumn(format="$%,.2f"),
                    "Date": st.column_config.DateColumn(format="MMM D"),
                },
            )
            # Deep link: ?item=<source_id> opens that exception (shareable with a colleague).
            linked_id = st.query_params.get("item")
            default = next((i for i, r in enumerate(rows) if r["source_id"] == linked_id), 0)
            picked = event.selection.rows[0] if event.selection.rows else default
            current: dict[str, Any] | None = rows[picked]
            st.query_params["item"] = rows[picked]["source_id"]

    with right:
        if rows:
            e = current or rows[0]
            code = e["exception_code"]
            label, sev, what, action = CODES.get(code, (code, "warning", "", ""))
            channel = "Card" if e["source"] == "stripe_charge" else "Bank"
            st.html(f"""
<div style="display:flex;gap:8px;align-items:center;margin-bottom:6px">
  {code_chip(code)}<span class="chip num">{money(e['amount_cents'])}</span>
  <span class="chip">{channel} · {short_date(e['cash_date'])}</span>
</div>
<div class="explain"><b>{what}</b><div class="act">Recommended: {action}</div></div>""")
            st.html(kv_card("Cash", [
                ("Source", "Stripe charge" if e["source"] == "stripe_charge" else "Bank receipt"),
                ("ID", f"<code>{e['source_id']}</code>"),
                ("Received", short_date(e["cash_date"])),
                ("Amount", money(e["amount_cents"])),
                ("Text", (e["cash_text"] or "–").strip()),
                ("Age", f"{e['age_days']} days"),
            ]))
            linked = [n.strip() for n in (e["linked_invoices"] or "").split(",") if n.strip()]
            candidates = data.invoices(linked or e["suggestion"])
            for inv in candidates:
                gap = e["amount_cents"] - inv["amount_residual_cents"]
                st.html(kv_card(
                    "Linked invoice" if linked else "Suggested invoice", [
                        ("Invoice", f"<b>{inv['invoice_number']}</b>"),
                        ("Customer", inv["partner_name"]),
                        ("Issued / due", f"{short_date(inv['invoice_date'])} / "
                                         f"{short_date(inv['due_date'])}"),
                        ("Open in ERP", money(inv["amount_residual_cents"])),
                        ("Cash vs open", f'<span class="diff">{money(gap, signed=True)}</span>'),
                    ]))
            if e["breakdown"] and "total" in e["breakdown"]:
                st.html(f'<div class="card"><h4>Why this candidate</h4>'
                        f'{meters(e["breakdown"])}</div>')

            if e["review_action"]:
                st.html(f'<div class="reviewed">{e["review_action"].capitalize()} by '
                        f'{e["reviewer"]} · {e["decided_at"]:%b %-d %H:%M}'
                        f'{" · " + e["review_comment"] if e["review_comment"] else ""}</div>')
            else:
                with st.form(key=f"review-{e['source_id']}", border=False):
                    target = st.selectbox(
                        "Apply to", [c["invoice_number"] for c in candidates] or ["—"],
                        disabled=not candidates)
                    comment = st.text_input("Comment",
                                            placeholder="Why? (kept in the audit trail)")
                    c1, c2 = st.columns(2)
                    accept = c1.form_submit_button("Accept", type="primary",
                                                   use_container_width=True)
                    reject = c2.form_submit_button("Reject", use_container_width=True)
                if accept or reject:
                    data.record_review(
                        source=e["source"], source_id=e["source_id"],
                        action="accepted" if accept else "rejected",
                        invoice_number=target if accept and candidates else None,
                        comment=comment, reviewer=st.session_state.get("reviewer", "jan"))
                    st.toast(f"{'Accepted' if accept else 'Rejected'} · saved to audit trail")
                    st.rerun()

# ------------------------------------------------------------------ matched
with tab_matched:
    rows_m = data.matched()
    by_tier: dict[int, int] = defaultdict(int)
    for r in rows_m:
        by_tier[r["tier"]] += 1
    st.html('<div style="display:flex;gap:8px;margin:4px 0 12px">' + "".join(
        f'{tier_chip(t)}<span class="chip num">{n}</span>'
        for t, n in sorted(by_tier.items())) + "</div>")
    st.dataframe([{
        "Tier": f"{r['tier']} · {TIER_LABEL[r['tier']]}",
        "Score": r["score"],
        "Date": r["cash_date"],
        "Source": "Card" if r["source"] == "stripe_charge" else "Bank",
        "Invoice": r["invoice_number"],
        "Customer": r["partner_name"],
        "Cash": r["amount_cents"] / 100,
        "Applied": (r["applied_cents"] or 0) / 100,
        "Status": "Auto" if r["status"] == "auto_matched" else "Needs review",
    } for r in rows_m], hide_index=True, use_container_width=True, height=520,
        column_config={
            "Score": st.column_config.ProgressColumn(min_value=0, max_value=1, format="%.2f"),
            "Cash": st.column_config.NumberColumn(format="$%,.2f"),
            "Applied": st.column_config.NumberColumn(format="$%,.2f"),
            "Date": st.column_config.DateColumn(format="MMM D"),
        })

# ------------------------------------------------------------------ journal
with tab_journal:
    lines = data.journal()
    dr = sum(r["debit_cents"] for r in lines)
    cr = sum(r["credit_cents"] for r in lines)
    entries = len({r["entry_id"] for r in lines})
    st.html(f'<div class="explain"><b>{entries} suggested entries, never posted '
            f"automatically.</b><div class='act'>Debits {money(dr)} = credits {money(cr)} "
            f"{ICONS['good'] if dr == cr else '✗'} · every entry balances (dbt test)."
            "</div></div>")
    st.dataframe([{
        "Entry": r["entry_id"], "Date": r["entry_date"],
        "Account": f"{r['account']} {r['account_name']}",
        "Debit": r["debit_cents"] / 100 if r["debit_cents"] else None,
        "Credit": r["credit_cents"] / 100 if r["credit_cents"] else None,
        "Memo": r["memo"],
    } for r in lines], hide_index=True, use_container_width=True, height=520,
        column_config={
            "Debit": st.column_config.NumberColumn(format="$%,.2f"),
            "Credit": st.column_config.NumberColumn(format="$%,.2f"),
            "Date": st.column_config.DateColumn(format="MMM D"),
        })

# ------------------------------------------------------------------ payouts
with tab_payouts:
    rows_p = data.payouts()
    if not rows_p:
        st.html('<div class="empty"><b>No Stripe payouts yet.</b><br>Stripe pays out daily '
                "with a two-day settlement delay. Each payout is checked twice: its contents "
                "must add up to the cent, and the deposit must reach the bank.</div>")
    else:
        st.dataframe([{
            "Payout": r["payout_id"], "Arrival": r["arrival_date"],
            "Amount": r["amount_cents"] / 100,
            "Components": r["component_count"],
            "Adds up": "✓" if r["components_net_cents"] == r["amount_cents"] else "✗",
            "In bank": "✓" if r["tier"] == 1 else (r["exception_code"] or "pending"),
        } for r in rows_p], hide_index=True, use_container_width=True,
            column_config={"Amount": st.column_config.NumberColumn(format="$%,.2f")})

st.html('<div class="foot">Read-only on Odoo. Review decisions are stored with reviewer and '
        "timestamp in <code>engine.reviews</code>. Theme: ⋮ → Settings.</div>")
