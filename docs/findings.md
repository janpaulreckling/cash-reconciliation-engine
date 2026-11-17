# Findings register

Defects and risks found while building and reviewing the engine, with the fix and the test that keeps each one fixed. Severity is the business impact if it had shipped: **High** means cash applied wrongly, data lost or a number misreported. **Medium** means a run aborts or a control is weakened. **Low** means cosmetic or a safe failure.

## Fixed

| # | Finding | Severity | Fix | Test |
|---|---|---|---|---|
| F1 | A payment larger than the open amount on the invoice it cites was absorbed silently; the excess vanished into Stripe clearing | High | Flag `OVERPAYMENT`, apply only the open amount, book the excess to Unapplied Cash | `test_card_overpayment_on_a_cited_invoice_is_flagged_not_absorbed`, `test_bank_overpayment_within_tolerance_is_still_flagged` |
| F2 | Tier 2 could offer an invoice an exact match had just settled in the same run | High | Score against what is still open in the run; a second payment for a settled invoice is `DUPLICATE_PAYMENT` | `test_tier1_is_never_blocked_by_a_weaker_match_seen_earlier` |
| F3 | Evaluation checked "invoice linked at the right tier", not "linked to its own cash", so a wrong-customer match would have scored as correct | High (measurement) | Score cash-invoice pairs against `expected_cash.csv` | `test_swapped_cash_is_a_precision_miss_even_though_tiers_look_right` |
| F4 | Precision for one scenario set also counted the other set's links | Medium (measurement) | Only pairs touching the evaluated set count | `test_pairs_of_another_scenario_set_do_not_count` |
| F5 | Subset-sum search (one wire, many invoices) was unbounded: 80 open invoices of one customer meant ~300M combinations and an apparent hang | High (availability) | Hard limits (20 candidates, 100k combinations); past them → `SEARCH_LIMIT` review. The candidate list is never truncated, because a truncated search can report a false "unique" subset | `test_subset_search_is_bounded_and_falls_back_to_review` (timed out at 45 s before the fix) |
| F6 | Amount fit one invoice while the memo named another open invoice of the same customer, and the engine applied by amount (found by the holdout) | High | `REFERENCE_CONFLICT` → review | `test_conflicting_reference_goes_to_review` (failed before the fix) |
| F7 | The incremental ERP extract filtered on `posted`, so an invoice cancelled later stayed "posted" forever | High | Extract all states and filter in staging after taking the latest version | `test_posted_invoice_cancelled_later_reaches_raw`, dbt unit test `cancelled_invoice_does_not_come_back_as_posted` |
| F8 | Offset paging: an invoice paid during the extract shifted every later row and one row was skipped for good | High | Keyset paging on `(write_date, id)` | `test_invoice_paid_during_extract_does_not_make_another_row_vanish` |
| F9 | Asking Stripe for a manual payout's components is an invalid request and would have aborted the whole extract | Medium | Ask only for automatic payouts; manual ones → `PAYOUT_NO_COMPONENTS` | `test_manual_payouts_are_not_asked_for_components` |
| F10 | Bank receipts matched with an exception got no journal suggestion | Medium | Journal lines for every linked receipt; any excess goes to Unapplied Cash | dbt: `assert_journal_entries_balance`, `assert_stripe_clearing_ties_to_stripe_balance` |
| F11 | The cloud demo built its database in-process, and renaming the file dropped writes still in DuckDB's WAL | Medium | Build in a child process, then rename atomically | Manual: fresh-start check of the cloud app (no automated test) |
| F12 | A demo database built by `recon demo` had no snapshot stamp, and the app crashed opening it | Low | `recon demo` writes the stamp; a missing stamp means stale | Manual: local app check (no automated test) |
| F13 | Plaid sandbox silently dropped transactions posted right after an Item was created | Medium (data) | Wait for `HISTORICAL_UPDATE_COMPLETE` before posting | `test_seeder_posts_in_small_batches_and_only_once` |
| F14 | A setup connectivity probe appeared as a customer payment | Low | Excluded on the record (`excluded_stripe_charges.csv`); raw and snapshot unchanged | dbt `unique_excluded_stripe_charges_charge_id`; clearing tie-out test covers the exclusion |

## False-positive traps checked (already handled, now pinned by tests)

| Trap | Result | Test |
|---|---|---|
| Two customers with similar names, same amount | Named customer matched; a partial name goes to review | `test_similar_names_pick_the_named_customer`, `test_ambiguous_partial_name_goes_to_review` |
| Stripe payout deposit that looks like a customer transfer | Classified `stripe_payout`, never matched to invoices | dbt unit test `bank_lines_are_classified_safely` |
| Refund or dispute counted as new incoming cash | Applies nothing; outgoing bank lines are never receipts | `test_refunded_or_disputed_card_payment_applies_nothing`, dbt unit test above |
| Several invoice combinations with the same total | `AMBIGUOUS`, nothing picked | `test_several_subsets_with_the_same_total_are_ambiguous` |
| Any cash item used twice across tiers; any invoice over-applied | Never, across 150 generated cases | property test `test_no_cash_used_twice_and_no_invoice_over_applied` (Hypothesis) |
| Paid in November, deposited in December | December deposit is outside the November close; a payout is not flagged before its arrival window | `test_payout_arriving_after_month_end_is_not_flagged_before_its_window`, dbt unit test above |

## Open (known, not yet fixed)

| # | Finding | Severity | Plan |
|---|---|---|---|
| O1 | Two open invoices of the same amount, no reference: the engine picks the one whose dates fit better instead of asking | Low (right invoice in the holdout, but a guess) | Treat date as a tie-breaker only when references also agree; evaluate on a fresh holdout |
| O2 | An abbreviated payer name (`PACIFIC CREST PROV`) isn't recognized | Low (safe failure: goes to review) | Customer alias table maintained by AR; evaluate on a fresh holdout |
| O3 | Stripe payouts not exercised with live data yet (the first ones arrive after the 2-day settlement delay) | Medium | Re-run extract and bank seed once payouts exist; the payout sum and deposit checks are unit-tested |
