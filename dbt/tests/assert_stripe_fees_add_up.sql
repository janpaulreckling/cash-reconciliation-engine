-- Every Stripe balance transaction must satisfy amount - fee = net.
select balance_transaction_id
from {{ ref('stg_stripe__balance_transactions') }}
where amount_cents - fee_cents <> net_cents
