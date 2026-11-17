-- A cash item is either placed once (possibly across several invoices in tier 3) or
-- not at all: never matched by two different rules.
select source, source_id
from {{ ref('fct_cash_decisions') }}
group by source, source_id
having count(distinct coalesce(rule, '')) > 1
