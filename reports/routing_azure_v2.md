# Routing

What the supervisor decided, scored against what the request and the books together called for.

| supervisor | n | fully correct | exact route | over | under | early stop | wasted calls |
|---|---|---|---|---|---|---|---|
| `azure` | 80 | 4% | 8% | 81% | 11% | 13% (15) | 103 |

Both sides of each pair, because reporting one side is how a routing number flatters a system: a supervisor that refuses everything has a perfect refusal rate, and one that asks about everything has a perfect clarification rate.

| supervisor | refuse | false refuse | clarify | false clarify | date | decline | relayed | plan-caused | retried ok | corrected |
|---|---|---|---|---|---|---|---|---|---|---|
| `azure` | 75% | 38% | 58% | 18% | 100% | 83% | 2 | 21 | 0 | 4 |

| supervisor | model calls | hit cap | backend errors | isolation violations | rejections by gate |
|---|---|---|---|---|---|
| `azure` | 399 | 3 | 0 | 0 | decision_invalid 61, step_cap 68 |

## `azure` by intent

| intent | n | fully correct | exact route | early stop |
|---|---|---|---|---|
| ambiguous_date | 6 | 33% | 33% | - |
| compare_only | 6 | 0% | 0% | - |
| cross_account | 6 | 0% | 0% | - |
| date_with_no_data | 6 | 0% | 33% | 33% (6) |
| full_reconcile | 12 | 0% | 0% | 0% (6) |
| investigate_no_stage | 8 | 0% | 0% | 0% (3) |
| notes_check | 6 | 0% | 0% | - |
| out_of_scope_capability | 6 | 0% | 17% | - |
| out_of_scope_execute | 6 | 0% | 0% | - |
| premature_stage | 6 | 0% | 0% | - |
| rerun_compare | 6 | 0% | 0% | - |
| underspecified_date | 6 | 17% | 17% | - |

