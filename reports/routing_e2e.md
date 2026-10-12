# Routing

What the supervisor decided, scored against what the request and the books together called for.

| supervisor | n | fully correct | exact route | over | under | early stop | wasted calls |
|---|---|---|---|---|---|---|---|
| `azure:gpt-5.4-mini @ azure-gpt-5.4-mini-90f56f` | 80 | 68% | 70% | 12% | 18% | 100% (15) | 15 |

Both sides of each pair, because reporting one side is how a routing number flatters a system: a supervisor that refuses everything has a perfect refusal rate, and one that asks about everything has a perfect clarification rate.

| supervisor | refuse | false refuse | clarify | false clarify | date | decline | relayed | plan-caused | retried ok | corrected |
|---|---|---|---|---|---|---|---|---|---|---|
| `azure:gpt-5.4-mini @ azure-gpt-5.4-mini-90f56f` | 100% | 9% | 25% | 3% | 95% | 100% | 6 | 11 | 0 | 0 |

| supervisor | model calls | hit cap | backend errors | isolation violations | rejections by gate |
|---|---|---|---|---|---|
| `azure:gpt-5.4-mini @ azure-gpt-5.4-mini-90f56f` | 167 | 0 | 0 | 1 | account_binding 1 |

## `azure:gpt-5.4-mini @ azure-gpt-5.4-mini-90f56f` by intent

| intent | n | fully correct | exact route | early stop |
|---|---|---|---|---|
| ambiguous_date | 6 | 0% | 0% | - |
| compare_only | 6 | 100% | 100% | - |
| cross_account | 6 | 0% | 0% | - |
| date_with_no_data | 6 | 67% | 100% | 100% (6) |
| full_reconcile | 12 | 92% | 92% | 100% (6) |
| investigate_no_stage | 8 | 88% | 88% | 100% (3) |
| notes_check | 6 | 100% | 100% | - |
| out_of_scope_capability | 6 | 83% | 83% | - |
| out_of_scope_execute | 6 | 100% | 100% | - |
| premature_stage | 6 | 0% | 0% | - |
| rerun_compare | 6 | 100% | 100% | - |
| underspecified_date | 6 | 50% | 50% | - |

