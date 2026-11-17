-- invoice_origin lists the sale order(s) an invoice bills, comma-separated on
-- combined invoices, so invoice <-> order is n:m.
select
    i.invoice_id,
    trim(o.order_name) as order_name
from {{ ref('stg_erp__invoices') }} i,
     unnest(string_split(i.invoice_origin, ',')) as o(order_name)
where i.invoice_origin is not null
