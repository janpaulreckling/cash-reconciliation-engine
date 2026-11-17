with latest as ({{ latest_version(source('raw', 'stripe_charges')) }})

select
    id                                        as charge_id,
    payment_intent                            as payment_intent_id,
    customer                                  as customer_id,
    amount                                    as amount_cents,
    coalesce(amount_refunded, 0)              as amount_refunded_cents,
    currency,
    created                                   as created_at,
    status,
    coalesce(disputed, false)                 as is_disputed,
    billing_name,
    billing_email,
    description,
    metadata ->> 'erp_invoice_number'         as invoice_ref,
    metadata ->> 'sf_opportunity_id'          as opportunity_ref,
    balance_transaction                       as balance_transaction_id
from latest
where status = 'succeeded'
  -- documented exclusions (setup/test traffic), see seeds/excluded_stripe_charges.csv
  and id not in (select charge_id from {{ ref('excluded_stripe_charges') }})
