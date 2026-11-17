-- One row per cash item with what the engine decided and the evidence behind it.
-- The exception queue and the close status both read from here.
with d as (select * from {{ source('engine', 'current_decisions') }}),

cash as (
    select 'stripe_charge' as source, charge_id as source_id, created_at::date as cash_date,
           amount_cents, coalesce(billing_name, '') || ' ' || coalesce(description, '') as cash_text,
           invoice_ref as cash_reference
    from {{ ref('stg_stripe__charges') }}
    union all
    select 'bank_receipt', bank_transaction_id, posted_date, amount_cents, description, null
    from {{ ref('int_bank__classified') }} where cash_class in ('customer_receipt', 'stripe_payout')
    union all
    select 'stripe_payout', payout_id, arrival_date, amount_cents, 'Stripe payout', null
    from {{ ref('stg_stripe__payouts') }}
)

select
    d.source,
    d.source_id,
    c.cash_date,
    c.amount_cents,
    c.cash_text,
    c.cash_reference,
    d.tier,
    d.score,
    d.rule,
    d.invoice_number,
    i.partner_name,
    i.amount_residual_cents                         as invoice_open_cents,
    d.applied_cents,
    d.writeoff_cents,
    d.exception_code,
    d.suggestion,
    d.breakdown,
    case
        when d.exception_code is null and d.tier is not null then 'auto_matched'
        when d.tier is not null then 'matched_needs_review'
        else 'exception'
    end                                             as status,
    d.run_id
from d
left join cash c on c.source = d.source and c.source_id = d.source_id
left join {{ ref('stg_erp__invoices') }} i on i.invoice_number = d.invoice_number
