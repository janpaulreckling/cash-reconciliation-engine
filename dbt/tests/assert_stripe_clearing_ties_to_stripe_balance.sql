-- The control behind the "Stripe clearing" tile: the suggested entries must leave
-- Stripe Clearing (1150) equal to what Stripe holds for the merchant, i.e. the net of
-- every balance transaction except payouts, less the payouts already booked to the bank.
with clearing as (
    select coalesce(sum(debit_cents - credit_cents), 0) as cents
    from {{ ref('fct_journal_suggestions') }} where account = '1150'
),
stripe as (
    select coalesce(sum(net_cents) filter (where type not in ('payout', 'payout_cancel',
                                                               'payout_failure')), 0)
         - coalesce((select sum(amount_cents) from {{ ref('fct_cash_decisions') }}
                     where source = 'stripe_payout' and rule = 'payout-deposit'
                       and tier = 1), 0) as cents
    from {{ ref('stg_stripe__balance_transactions') }}
)
select clearing.cents as clearing_cents, stripe.cents as stripe_cents
from clearing, stripe
where clearing.cents <> stripe.cents
  -- card payments enter the journal through match decisions: no run, nothing to tie
  and exists (select 1 from {{ source('engine', 'match_runs') }})
