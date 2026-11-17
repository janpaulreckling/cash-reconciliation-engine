-- A2 control: every automatic payout equals the net of what Stripe swept into it
-- (charges - fees - refunds +/- adjustments), to the cent. Manual payouts have no
-- component list (Stripe limitation) and are flagged by the matching engine instead.
select payout_id, payout_cents, components_net_cents
from {{ ref('int_stripe__payout_components') }}
where automatic and payout_cents <> components_net_cents
