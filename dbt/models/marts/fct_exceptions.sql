-- The review queue: one row per cash item that needs a human, with the money at stake.
select
    source,
    source_id,
    any_value(cash_date)                            as cash_date,
    any_value(amount_cents)                         as amount_cents,
    any_value(cash_text)                            as cash_text,
    any_value(exception_code)                       as exception_code,
    any_value(status)                               as status,
    max(score)                                      as best_score,
    any_value(suggestion)                           as suggestion,
    any_value(breakdown)                            as breakdown,
    string_agg(invoice_number, ', ')                as linked_invoices,
    case any_value(exception_code)
        when 'DISPUTED' then 1 when 'PAYOUT_SUM_BREAK' then 1 when 'PAYOUT_NOT_IN_BANK' then 1
        when 'DUPLICATE_PAYMENT' then 2 when 'OVERPAYMENT' then 2 when 'AMOUNT_MISMATCH' then 2
        when 'UNAPPLIED_CASH' then 2 when 'NO_CANDIDATE' then 2 when 'AMBIGUOUS' then 2
        else 3
    end                                             as priority,  -- 1 = money at risk
    date_diff('day', any_value(cash_date), current_date) as age_days
from {{ ref('fct_cash_decisions') }}
where exception_code is not null
group by source, source_id
