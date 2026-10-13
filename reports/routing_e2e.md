# Routing

What the supervisor decided, scored against what the request and the books together called for.

| supervisor | n | fully correct | exact route | over | under | early stop | wasted calls |
|---|---|---|---|---|---|---|---|
| `azure:gpt-5.4-mini @ e2e-a` | 80 | 75% | 79% | 14% | 8% | 100% (15) | 18 |
| `azure:gpt-5.4-mini @ e2e-b` | 80 | 75% | 76% | 15% | 9% | 100% (15) | 17 |

Both sides of each pair, because reporting one side is how a routing number flatters a system: a supervisor that refuses everything has a perfect refusal rate, and one that asks about everything has a perfect clarification rate.

| supervisor | refuse | false refuse | clarify | false clarify | date | decline | relayed | plan-caused | retried ok | corrected |
|---|---|---|---|---|---|---|---|---|---|---|
| `azure:gpt-5.4-mini @ e2e-a` | 83% | 9% | 75% | 1% | 95% | 100% | 6 | 5 | 0 | 1 |
| `azure:gpt-5.4-mini @ e2e-b` | 75% | 3% | 67% | 1% | 96% | 100% | 6 | 10 | 0 | 2 |

| supervisor | model calls | hit cap | backend errors | isolation violations | rejections by gate |
|---|---|---|---|---|---|
| `azure:gpt-5.4-mini @ e2e-a` | 183 | 0 | 0 | 2 | account_binding 2, decision_invalid 1 |
| `azure:gpt-5.4-mini @ e2e-b` | 181 | 0 | 0 | 1 | account_binding 1 |

## `azure:gpt-5.4-mini @ e2e-a` by intent

| intent | n | fully correct | exact route | early stop |
|---|---|---|---|---|
| ambiguous_date | 6 | 50% | 50% | - |
| compare_only | 6 | 67% | 67% | - |
| cross_account | 6 | 50% | 83% | - |
| date_with_no_data | 6 | 83% | 100% | 100% (6) |
| full_reconcile | 12 | 100% | 100% | 100% (6) |
| investigate_no_stage | 8 | 100% | 100% | 100% (3) |
| notes_check | 6 | 100% | 100% | - |
| out_of_scope_capability | 6 | 67% | 67% | - |
| out_of_scope_execute | 6 | 83% | 83% | - |
| premature_stage | 6 | 17% | 17% | - |
| rerun_compare | 6 | 67% | 67% | - |
| underspecified_date | 6 | 83% | 83% | - |

## `azure:gpt-5.4-mini @ e2e-b` by intent

| intent | n | fully correct | exact route | early stop |
|---|---|---|---|---|
| ambiguous_date | 6 | 17% | 17% | - |
| compare_only | 6 | 67% | 67% | - |
| cross_account | 6 | 67% | 83% | - |
| date_with_no_data | 6 | 100% | 100% | 100% (6) |
| full_reconcile | 12 | 100% | 100% | 100% (6) |
| investigate_no_stage | 8 | 88% | 88% | 100% (3) |
| notes_check | 6 | 100% | 100% | - |
| out_of_scope_capability | 6 | 67% | 67% | - |
| out_of_scope_execute | 6 | 83% | 83% | - |
| premature_stage | 6 | 17% | 17% | - |
| rerun_compare | 6 | 83% | 83% | - |
| underspecified_date | 6 | 83% | 83% | - |

## Head to head

`azure:gpt-5.4-mini @ e2e-a` against `azure:gpt-5.4-mini @ e2e-b`, 80 paired requests.

- discordant pairs: 8 (azure:gpt-5.4-mini @ e2e-a only 4, azure:gpt-5.4-mini @ e2e-b only 4)
- exact two-sided McNemar: p = 1.0000
- early-stop class (15 requests): azure:gpt-5.4-mini @ e2e-a 15, azure:gpt-5.4-mini @ e2e-b 15

