with latest as ({{ latest_version(source('raw', 'stripe_disputes')) }})

select id as dispute_id, charge as charge_id, amount as amount_cents, currency,
       created as created_at, status, reason
from latest
