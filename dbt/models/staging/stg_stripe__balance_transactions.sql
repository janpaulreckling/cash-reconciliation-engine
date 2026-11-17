with latest as ({{ latest_version(source('raw', 'stripe_balance_transactions')) }})

select
    id              as balance_transaction_id,
    type,
    reporting_category,
    source          as source_id,
    amount          as amount_cents,
    fee             as fee_cents,
    net             as net_cents,
    currency,
    created         as created_at,
    available_on,
    status,
    payout          as payout_id
from latest
where coalesce(source, '') not in (select charge_id from {{ ref('excluded_stripe_charges') }})
