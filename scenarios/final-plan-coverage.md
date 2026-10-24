# Final scenario plan coverage

250 grounded plans prepared offline. Customer messages, final human review, and application execution are pending.

Source: clean pre-pilot database snapshot; world date 2026-07-01. Selected agent model: gpt-4o-mini.

## Intent allocation

| Intent | Coverage | Challenge | Total |
| --- | ---: | ---: | ---: |
| account_change | 6 | 0 | 6 |
| cancellation | 20 | 0 | 20 |
| dispute | 8 | 0 | 8 |
| order_status | 35 | 23 | 58 |
| out_of_scope | 6 | 0 | 6 |
| policy_question | 25 | 4 | 29 |
| product_search | 20 | 15 | 35 |
| refund | 35 | 16 | 51 |
| return_deadline | 20 | 17 | 37 |
| **Total** | **175** | **75** | **250** |

## role

| Value | Coverage | Challenge |
| --- | ---: | ---: |
| merchant | 62 | 13 |
| shopper | 60 | 54 |
| support | 53 | 8 |

## turn_count

| Value | Coverage | Challenge |
| --- | ---: | ---: |
| 1 | 74 | 6 |
| 2 | 67 | 32 |
| 3 | 17 | 21 |
| 4 | 17 | 16 |

## user_style

| Value | Coverage | Challenge |
| --- | ---: | ---: |
| confused_rambling | 22 | 9 |
| frustrated_impatient | 22 | 9 |
| neutral_conversational | 22 | 10 |
| operational_shorthand | 22 | 9 |
| repetitive_pressuring | 22 | 9 |
| requests_short_plain_answer | 22 | 9 |
| terse_fragmentary | 22 | 10 |
| typo_heavy | 21 | 10 |

## tools_needed

| Value | Coverage | Challenge |
| --- | ---: | ---: |
| none | 6 | 0 |
| one_lookup | 37 | 15 |
| several_calls | 132 | 60 |

## difficulty

| Value | Coverage | Challenge |
| --- | ---: | ---: |
| ambiguous | 0 | 5 |
| boundary | 0 | 32 |
| conflicting_information | 0 | 15 |
| correction_across_turns | 0 | 8 |
| missing_information | 0 | 15 |
| well_specified | 175 | 0 |

## Challenge allocation

- return_window_boundary: 8
- store_return_override: 8
- refund_amount_boundary: 12
- correction_across_turns: 8
- authorization_boundary: 5
- restocking_condition: 4
- data_quality: 30

## Damaged records

- dq-order-missing-delivery-date: 5
- dq-order-reversed-dates: 5
- dq-order-store-mismatch: 5
- dq-product-duplicate-title: 5
- dq-product-invalid-price: 5
- dq-product-missing-title: 5

## Grounding checks

```json
{
  "authorized_order_lookups": 177,
  "valid_generation_briefs": 250,
  "catalogue_lookups": 35,
  "unique_ordinary_order_records": 162,
  "distinct_refund_or_cancellation_targets": 48,
  "all_checks_passed": true,
  "mode": "offline_read_only_sources",
  "official_scenario_validator": "pending: customer messages do not exist yet"
}
```

Planned user messages: 524 (not generated yet).

Refund/cancellation outcomes are expectations, not observed results. The final scenario validator has not run because no final customer messages exist yet.

## Next step

Prepare the separate customer-conversation generation step for approval; then run the independent critic, validate the completed scenarios, and pause for the student's 15 reviews. Preserve the pilot assessments. The final run, monitoring subset, trace export, smoke report, and student video remain pending.
