-- One row: where does the November close stand?
with d as (select * from {{ ref('fct_cash_decisions') }} where source <> 'stripe_payout'),
cash_items as (
    select source, source_id, any_value(amount_cents) as amount, any_value(status) as status
    from d group by all
),
je as (select * from {{ ref('fct_journal_suggestions') }}),
inv as (
    select count(*) as n, sum(amount_residual_cents) as open_cents
    from {{ ref('stg_erp__invoices') }}
    where move_type = 'out_invoice' and amount_residual_cents > 0
)

select
    cast('{{ var("close_period_start") }}' as date)                   as period_start,
    cast('{{ var("close_period_end") }}' as date)                     as period_end,
    (select n from inv)                                               as open_invoices,
    (select open_cents from inv)                                      as open_ar_cents,
    count(*)                                                          as cash_items,
    sum(amount)                                                       as cash_cents,
    count(*) filter (where status = 'auto_matched')                   as auto_matched_items,
    round(count(*) filter (where status = 'auto_matched') / count(*), 4) as auto_match_rate,
    sum(amount) filter (where status <> 'auto_matched')               as cash_needing_review_cents,
    (select count(*) from {{ ref('fct_exceptions') }})                as open_exceptions,
    (select sum(applied_cents) from d)                                as cash_applied_cents,
    (select sum(debit_cents - credit_cents) from je where account = '1150') as stripe_clearing_cents,
    (select sum(credit_cents - debit_cents) from je where account = '2050') as unapplied_cash_cents,
    (select max(started_at) from {{ source('engine', 'match_runs') }}) as matched_at
from cash_items
