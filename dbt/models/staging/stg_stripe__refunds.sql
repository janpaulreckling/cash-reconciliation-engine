with latest as ({{ latest_version(source('raw', 'stripe_refunds')) }})

select id as refund_id, charge as charge_id, amount as amount_cents, currency,
       created as created_at, status, reason, balance_transaction as balance_transaction_id
from latest
