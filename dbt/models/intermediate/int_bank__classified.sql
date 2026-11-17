-- What kind of cash is each bank line? Only customer receipts take part in invoice
-- matching. Classification uses maintained bank rules first (like the bank rules in an
-- accounting system), then Plaid's category, then the sign. Anything else coming in is
-- treated as customer cash, even if it can't be identified yet: that is exactly what
-- the exception queue is for.
with rules as (select * from {{ ref('bank_rules') }}),

ruled as (
    select
        t.*,
        r.rule_id,
        r.cash_class as rule_class,
        row_number() over (partition by t.bank_transaction_id order by r.rule_id) as rn
    from {{ ref('stg_bank__transactions') }} t
    left join rules r on regexp_matches(t.description, r.pattern)
)

select
    * exclude (rule_class, rn),
    case
        when rule_class is not null then rule_class
        when amount_cents > 0
             and coalesce(category_detailed, '') in ('INCOME_WAGES', 'INCOME_INTEREST_EARNED')
            then 'other'
        when amount_cents > 0 then 'customer_receipt'
        else 'other'
    end as cash_class,
    posted_date between cast('{{ var("close_period_start") }}' as date)
                    and cast('{{ var("close_period_end") }}' as date) as in_close_period
from ruled
where rn = 1
