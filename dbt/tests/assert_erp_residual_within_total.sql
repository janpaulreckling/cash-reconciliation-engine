-- An invoice can't have more open than it was issued for, or a negative balance.
select invoice_number
from {{ ref('stg_erp__invoices') }}
where move_type = 'out_invoice'
  and (amount_residual_cents < 0 or amount_residual_cents > amount_total_cents)
