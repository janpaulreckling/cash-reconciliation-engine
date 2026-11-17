-- Double entry: debits = credits within every suggested journal entry.
select entry_id, sum(debit_cents) as dr, sum(credit_cents) as cr
from {{ ref('fct_journal_suggestions') }}
group by entry_id
having sum(debit_cents) <> sum(credit_cents)
