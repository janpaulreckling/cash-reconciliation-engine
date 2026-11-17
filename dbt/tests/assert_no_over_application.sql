-- No invoice may receive more cash than was open on it (write-offs included).
select invoice_number, max(invoice_open_cents) as open_cents,
       sum(applied_cents + coalesce(writeoff_cents, 0)) as applied
from {{ ref('fct_cash_decisions') }}
where invoice_number is not null
group by invoice_number
having sum(applied_cents + coalesce(writeoff_cents, 0)) > max(invoice_open_cents)
