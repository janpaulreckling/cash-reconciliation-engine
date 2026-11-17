with latest as ({{ latest_version(source('raw', 'stripe_payouts')) }})

select
    id                      as payout_id,
    amount                  as amount_cents,
    currency,
    created                 as created_at,
    arrival_date::date      as arrival_date,
    status,
    automatic,
    trace_id,
    balance_transaction     as balance_transaction_id
from latest
