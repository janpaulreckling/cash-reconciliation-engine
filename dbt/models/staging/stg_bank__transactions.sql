-- Operating account only, money IN positive (Plaid reports inflows as negative).
with latest as ({{ latest_version(source('raw', 'bank_transactions')) }}),
accounts as ({{ latest_version(source('raw', 'bank_accounts')) }})

select
    t.id                                                    as bank_transaction_id,
    t.date                                                  as posted_date,
    cast(round(-t.amount * 100) as bigint)                  as amount_cents,
    t.iso_currency_code                                     as currency,
    coalesce(t.original_description, t.name)                as description,
    t.category,
    t.category_detailed,
    t.pending
from latest t
join accounts a on a.id = t.account_id
where not t.removed
  and a.subtype = 'checking'
  and a.mask = '{{ var("operating_account_mask") }}'
