-- Current state of each posted customer invoice / credit note, money in cents.
with latest as (
    {{ latest_version(source('raw', 'erp_invoices'), order_by='write_date desc, _loaded_at desc') }}
)

select
    id                                              as invoice_id,
    name                                            as invoice_number,
    move_type,
    partner_id,
    partner_name,
    ref                                             as opportunity_id,
    payment_reference,
    invoice_origin,
    invoice_date,
    invoice_date_due                                as due_date,
    currency,
    cast(round(amount_total * 100) as bigint)       as amount_total_cents,
    cast(round(amount_residual * 100) as bigint)    as amount_residual_cents,
    payment_state,
    reversed_entry_id,
    write_date                                      as erp_updated_at
from latest
where state = 'posted'
