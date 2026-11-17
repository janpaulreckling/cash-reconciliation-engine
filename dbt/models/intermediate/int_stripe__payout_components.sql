-- A2 control input: what Stripe says went into each automatic payout.
select
    p.payout_id,
    p.amount_cents                                         as payout_cents,
    p.arrival_date,
    p.automatic,
    count(b.balance_transaction_id)                        as component_count,
    coalesce(sum(b.amount_cents) filter (where b.type = 'charge'), 0)  as gross_charges_cents,
    coalesce(sum(b.fee_cents), 0)                          as fees_cents,
    coalesce(sum(b.amount_cents) filter (where b.type = 'refund'), 0)  as refunds_cents,
    coalesce(sum(b.amount_cents) filter (where b.type = 'adjustment'), 0) as adjustments_cents,
    coalesce(sum(b.net_cents), 0)                          as components_net_cents
from {{ ref('stg_stripe__payouts') }} p
left join {{ ref('stg_stripe__balance_transactions') }} b on b.payout_id = p.payout_id
group by all
