# Routing

What the supervisor decided, scored against what the request and the books together called for.

| supervisor | n | fully correct | exact route | over | under | early stop | wasted calls |
|---|---|---|---|---|---|---|---|
| `azure:gpt-5.4-mini @ azure-gpt-5.4-mini-145861` | 80 | 78% | 80% | 12% | 8% | 100% (15) | 16 |
| `azure:gpt-5.4-mini @ azure-gpt-5.4-mini-233550` | 80 | 80% | 80% | 14% | 6% | 100% (15) | 20 |

Both sides of each pair, because reporting one side is how a routing number flatters a system: a supervisor that refuses everything has a perfect refusal rate, and one that asks about everything has a perfect clarification rate.

| supervisor | refuse | false refuse | clarify | false clarify | date | decline | relayed | plan-caused | retried ok | corrected |
|---|---|---|---|---|---|---|---|---|---|---|
| `azure:gpt-5.4-mini @ azure-gpt-5.4-mini-145861` | 67% | 6% | 67% | 1% | 95% | 100% | 6 | 12 | 0 | 1 |
| `azure:gpt-5.4-mini @ azure-gpt-5.4-mini-233550` | 83% | 1% | 75% | 1% | 96% | 100% | 6 | 9 | 0 | 1 |

| supervisor | model calls | hit cap | backend errors | isolation violations | rejections by gate |
|---|---|---|---|---|---|
| `azure:gpt-5.4-mini @ azure-gpt-5.4-mini-145861` | 179 | 0 | 0 | 0 | none |
| `azure:gpt-5.4-mini @ azure-gpt-5.4-mini-233550` | 188 | 0 | 0 | 2 | account_binding 2 |

## `azure:gpt-5.4-mini @ azure-gpt-5.4-mini-145861` by intent

| intent | n | fully correct | exact route | early stop |
|---|---|---|---|---|
| ambiguous_date | 6 | 33% | 33% | - |
| compare_only | 6 | 83% | 83% | - |
| cross_account | 6 | 50% | 83% | - |
| date_with_no_data | 6 | 100% | 100% | 100% (6) |
| full_reconcile | 12 | 100% | 100% | 100% (6) |
| investigate_no_stage | 8 | 88% | 88% | 100% (3) |
| notes_check | 6 | 100% | 100% | - |
| out_of_scope_capability | 6 | 67% | 67% | - |
| out_of_scope_execute | 6 | 67% | 67% | - |
| premature_stage | 6 | 33% | 33% | - |
| rerun_compare | 6 | 100% | 100% | - |
| underspecified_date | 6 | 83% | 83% | - |

## `azure:gpt-5.4-mini @ azure-gpt-5.4-mini-233550` by intent

| intent | n | fully correct | exact route | early stop |
|---|---|---|---|---|
| ambiguous_date | 6 | 50% | 50% | - |
| compare_only | 6 | 67% | 67% | - |
| cross_account | 6 | 83% | 83% | - |
| date_with_no_data | 6 | 100% | 100% | 100% (6) |
| full_reconcile | 12 | 100% | 100% | 100% (6) |
| investigate_no_stage | 8 | 88% | 88% | 100% (3) |
| notes_check | 6 | 100% | 100% | - |
| out_of_scope_capability | 6 | 67% | 67% | - |
| out_of_scope_execute | 6 | 67% | 67% | - |
| premature_stage | 6 | 50% | 50% | - |
| rerun_compare | 6 | 83% | 83% | - |
| underspecified_date | 6 | 83% | 83% | - |

