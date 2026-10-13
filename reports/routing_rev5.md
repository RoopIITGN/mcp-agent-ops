# Routing

What the supervisor decided, scored against what the request and the books together called for.

| supervisor | n | fully correct | exact route | over | under | early stop | wasted calls |
|---|---|---|---|---|---|---|---|
| `azure:gpt-5.4-mini @ azure-gpt-5.4-mini-57b8c8` | 80 | 66% | 76% | 15% | 9% | 100% (15) | 18 |
| `azure:gpt-5.4-mini @ azure-gpt-5.4-mini-5afc5f` | 80 | 68% | 76% | 14% | 10% | 100% (15) | 18 |

Both sides of each pair, because reporting one side is how a routing number flatters a system: a supervisor that refuses everything has a perfect refusal rate, and one that asks about everything has a perfect clarification rate.

| supervisor | refuse | false refuse | clarify | false clarify | date | decline | relayed | plan-caused | retried ok | corrected |
|---|---|---|---|---|---|---|---|---|---|---|
| `azure:gpt-5.4-mini @ azure-gpt-5.4-mini-57b8c8` | 75% | 15% | 67% | 7% | 95% | 100% | 6 | 12 | 0 | 1 |
| `azure:gpt-5.4-mini @ azure-gpt-5.4-mini-5afc5f` | 83% | 13% | 75% | 4% | 96% | 100% | 6 | 12 | 0 | 2 |

| supervisor | model calls | hit cap | backend errors | isolation violations | rejections by gate |
|---|---|---|---|---|---|
| `azure:gpt-5.4-mini @ azure-gpt-5.4-mini-57b8c8` | 181 | 0 | 0 | 2 | account_binding 2 |
| `azure:gpt-5.4-mini @ azure-gpt-5.4-mini-5afc5f` | 178 | 0 | 0 | 1 | account_binding 1 |

## `azure:gpt-5.4-mini @ azure-gpt-5.4-mini-57b8c8` by intent

| intent | n | fully correct | exact route | early stop |
|---|---|---|---|---|
| ambiguous_date | 6 | 33% | 33% | - |
| compare_only | 6 | 67% | 67% | - |
| cross_account | 6 | 33% | 67% | - |
| date_with_no_data | 6 | 0% | 100% | 100% (6) |
| full_reconcile | 12 | 100% | 100% | 100% (6) |
| investigate_no_stage | 8 | 88% | 88% | 100% (3) |
| notes_check | 6 | 83% | 83% | - |
| out_of_scope_capability | 6 | 67% | 67% | - |
| out_of_scope_execute | 6 | 83% | 83% | - |
| premature_stage | 6 | 33% | 33% | - |
| rerun_compare | 6 | 83% | 83% | - |
| underspecified_date | 6 | 83% | 83% | - |

## `azure:gpt-5.4-mini @ azure-gpt-5.4-mini-5afc5f` by intent

| intent | n | fully correct | exact route | early stop |
|---|---|---|---|---|
| ambiguous_date | 6 | 33% | 33% | - |
| compare_only | 6 | 67% | 67% | - |
| cross_account | 6 | 50% | 67% | - |
| date_with_no_data | 6 | 0% | 100% | 100% (6) |
| full_reconcile | 12 | 100% | 100% | 100% (6) |
| investigate_no_stage | 8 | 88% | 88% | 100% (3) |
| notes_check | 6 | 100% | 100% | - |
| out_of_scope_capability | 6 | 83% | 83% | - |
| out_of_scope_execute | 6 | 83% | 83% | - |
| premature_stage | 6 | 17% | 17% | - |
| rerun_compare | 6 | 67% | 67% | - |
| underspecified_date | 6 | 83% | 83% | - |

