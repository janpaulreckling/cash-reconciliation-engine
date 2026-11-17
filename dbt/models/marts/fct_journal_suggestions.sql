-- Suggested journal entries for the close. Generated, never posted: an accountant
-- reviews and books them. Every entry balances (tested), and Stripe Clearing (1150)
-- nets to what Stripe still holds once payouts are booked.
with decisions as (select * from {{ ref('fct_cash_decisions') }}),

charges as (
    select source_id, any_value(cash_date) as d, any_value(amount_cents) as amount,
           sum(coalesce(applied_cents, 0)) as applied, string_agg(invoice_number, ', ') as invs
    from decisions where source = 'stripe_charge' group by source_id
),

bt as (select * from {{ ref('stg_stripe__balance_transactions') }}),

lines as (
    -- Card payment: the full charge lands in clearing; what isn't applied to an
    -- invoice (refunded, disputed, duplicate, unknown) waits in Unapplied Cash.
    select 'CARD-' || source_id as entry_id, d as entry_date, '1150' as account,
           amount as debit_cents, 0 as credit_cents, 'Card payment ' || source_id as memo
    from charges
    union all
    select 'CARD-' || source_id, d, '1200', 0, applied, 'Applied to ' || coalesce(invs, '-')
    from charges where applied > 0
    union all
    select 'CARD-' || source_id, d, '2050', 0, amount - applied, 'Not applied, see exception queue'
    from charges where amount - applied > 0

    -- Stripe processing fees (charged as application fees on the merchant)
    union all
    select 'FEE-' || balance_transaction_id, created_at::date, '6150', fee_cents, 0,
           'Stripe fee on ' || source_id
    from bt where type = 'charge' and fee_cents > 0
    union all
    select 'FEE-' || balance_transaction_id, created_at::date, '1150', 0, fee_cents,
           'Stripe fee on ' || source_id
    from bt where type = 'charge' and fee_cents > 0

    -- Refunds give back cash that was parked in Unapplied Cash
    union all
    select 'REF-' || balance_transaction_id, created_at::date, '2050', -amount_cents, 0,
           'Refund ' || source_id
    from bt where type = 'refund'
    union all
    select 'REF-' || balance_transaction_id, created_at::date, '1150', 0, -amount_cents,
           'Refund ' || source_id
    from bt where type = 'refund'

    -- Disputes: Stripe withholds the amount plus a fee until the dispute is decided
    union all
    select 'DSP-' || balance_transaction_id, created_at::date, '1160', -amount_cents, 0,
           'Funds withheld for dispute ' || source_id
    from bt where type = 'adjustment' and amount_cents < 0
    union all
    select 'DSP-' || balance_transaction_id, created_at::date, '6170', fee_cents, 0,
           'Dispute fee ' || source_id
    from bt where type = 'adjustment' and fee_cents > 0
    union all
    select 'DSP-' || balance_transaction_id, created_at::date, '1150', 0, -net_cents,
           'Dispute ' || source_id
    from bt where type = 'adjustment' and net_cents < 0

    -- Bank receipts applied to invoices (small bank fees written off)
    union all
    select 'BNK-' || source_id, any_value(cash_date), '1010', any_value(amount_cents), 0,
           'Customer receipt ' || source_id
    from decisions where source = 'bank_receipt' and exception_code is null and tier is not null
    group by source_id
    union all
    select 'BNK-' || source_id, cash_date, '6160', writeoff_cents, 0, 'Bank fee short-paid'
    from decisions where source = 'bank_receipt' and coalesce(writeoff_cents, 0) > 0
    union all
    select 'BNK-' || source_id, cash_date, '1200', 0, applied_cents + coalesce(writeoff_cents, 0),
           'Applied to ' || invoice_number
    from decisions where source = 'bank_receipt' and exception_code is null and tier is not null

    -- Bank receipts nobody could place: book the cash, park it
    union all
    select 'UNA-' || source_id, cash_date, '1010', amount_cents, 0, 'Receipt ' || source_id
    from decisions where source = 'bank_receipt' and tier is null
      and exception_code not in ('DEPOSIT_WITHOUT_PAYOUT')
    union all
    select 'UNA-' || source_id, cash_date, '2050', 0, amount_cents, exception_code
    from decisions where source = 'bank_receipt' and tier is null
      and exception_code not in ('DEPOSIT_WITHOUT_PAYOUT')

    -- Stripe payouts that arrived in the bank
    union all
    select 'PAY-' || source_id, cash_date, '1010', amount_cents, 0, 'Stripe payout ' || source_id
    from decisions where source = 'stripe_payout' and rule = 'payout-deposit' and tier = 1
    union all
    select 'PAY-' || source_id, cash_date, '1150', 0, amount_cents, 'Stripe payout ' || source_id
    from decisions where source = 'stripe_payout' and rule = 'payout-deposit' and tier = 1
)

select l.*, a.name as account_name
from lines l
left join {{ ref('chart_of_accounts') }} a using (account)
where l.debit_cents <> 0 or l.credit_cents <> 0
