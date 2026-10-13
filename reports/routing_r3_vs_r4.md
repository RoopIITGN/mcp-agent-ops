# Routing

What the supervisor decided, scored against what the request and the books together called for.

| supervisor | n | fully correct | exact route | over | under | early stop | wasted calls |
|---|---|---|---|---|---|---|---|
| `azure:gpt-5.4-mini @ azure-gpt-5.4-mini-2d0d89` | 80 | 68% | 70% | 11% | 19% | 100% (15) | 13 |
| `azure:gpt-5.4-mini @ azure-gpt-5.4-mini-31daba` | 80 | 76% | 78% | 15% | 8% | 100% (15) | 14 |
| `azure:gpt-5.4-mini @ azure-gpt-5.4-mini-e1ba04` | 80 | 74% | 76% | 18% | 6% | 100% (15) | 19 |
| `azure:gpt-5.4-mini @ azure-gpt-5.4-mini-f67210` | 80 | 70% | 70% | 11% | 18% | 100% (15) | 11 |

Both sides of each pair, because reporting one side is how a routing number flatters a system: a supervisor that refuses everything has a perfect refusal rate, and one that asks about everything has a perfect clarification rate.

| supervisor | refuse | false refuse | clarify | false clarify | date | decline | relayed | plan-caused | retried ok | corrected |
|---|---|---|---|---|---|---|---|---|---|---|
| `azure:gpt-5.4-mini @ azure-gpt-5.4-mini-2d0d89` | 92% | 4% | 25% | 1% | 98% | 100% | 6 | 18 | 0 | 0 |
| `azure:gpt-5.4-mini @ azure-gpt-5.4-mini-31daba` | 83% | 3% | 33% | 3% | 95% | 100% | 6 | 18 | 0 | 0 |
| `azure:gpt-5.4-mini @ azure-gpt-5.4-mini-e1ba04` | 83% | 4% | 25% | 3% | 95% | 100% | 6 | 18 | 0 | 0 |
| `azure:gpt-5.4-mini @ azure-gpt-5.4-mini-f67210` | 100% | 4% | 25% | 0% | 98% | 100% | 6 | 18 | 0 | 0 |

| supervisor | model calls | hit cap | backend errors | isolation violations | rejections by gate |
|---|---|---|---|---|---|
| `azure:gpt-5.4-mini @ azure-gpt-5.4-mini-2d0d89` | 166 | 0 | 0 | 1 | account_binding 1 |
| `azure:gpt-5.4-mini @ azure-gpt-5.4-mini-31daba` | 179 | 0 | 0 | 2 | account_binding 2 |
| `azure:gpt-5.4-mini @ azure-gpt-5.4-mini-e1ba04` | 187 | 0 | 0 | 3 | account_binding 3 |
| `azure:gpt-5.4-mini @ azure-gpt-5.4-mini-f67210` | 172 | 0 | 0 | 1 | account_binding 1, decision_invalid 3 |

## `azure:gpt-5.4-mini @ azure-gpt-5.4-mini-2d0d89` by intent

| intent | n | fully correct | exact route | early stop |
|---|---|---|---|---|
| ambiguous_date | 6 | 0% | 0% | - |
| compare_only | 6 | 100% | 100% | - |
| cross_account | 6 | 17% | 33% | - |
| date_with_no_data | 6 | 100% | 100% | 100% (6) |
| full_reconcile | 12 | 67% | 67% | 100% (6) |
| investigate_no_stage | 8 | 88% | 88% | 100% (3) |
| notes_check | 6 | 100% | 100% | - |
| out_of_scope_capability | 6 | 83% | 100% | - |
| out_of_scope_execute | 6 | 100% | 100% | - |
| premature_stage | 6 | 0% | 0% | - |
| rerun_compare | 6 | 100% | 100% | - |
| underspecified_date | 6 | 50% | 50% | - |

## `azure:gpt-5.4-mini @ azure-gpt-5.4-mini-31daba` by intent

| intent | n | fully correct | exact route | early stop |
|---|---|---|---|---|
| ambiguous_date | 6 | 0% | 0% | - |
| compare_only | 6 | 83% | 83% | - |
| cross_account | 6 | 67% | 83% | - |
| date_with_no_data | 6 | 100% | 100% | 100% (6) |
| full_reconcile | 12 | 100% | 100% | 100% (6) |
| investigate_no_stage | 8 | 75% | 75% | 100% (3) |
| notes_check | 6 | 100% | 100% | - |
| out_of_scope_capability | 6 | 83% | 83% | - |
| out_of_scope_execute | 6 | 83% | 83% | - |
| premature_stage | 6 | 33% | 33% | - |
| rerun_compare | 6 | 100% | 100% | - |
| underspecified_date | 6 | 67% | 67% | - |

## `azure:gpt-5.4-mini @ azure-gpt-5.4-mini-e1ba04` by intent

| intent | n | fully correct | exact route | early stop |
|---|---|---|---|---|
| ambiguous_date | 6 | 0% | 0% | - |
| compare_only | 6 | 67% | 67% | - |
| cross_account | 6 | 50% | 67% | - |
| date_with_no_data | 6 | 83% | 100% | 100% (6) |
| full_reconcile | 12 | 100% | 100% | 100% (6) |
| investigate_no_stage | 8 | 75% | 75% | 100% (3) |
| notes_check | 6 | 100% | 100% | - |
| out_of_scope_capability | 6 | 83% | 83% | - |
| out_of_scope_execute | 6 | 83% | 83% | - |
| premature_stage | 6 | 67% | 67% | - |
| rerun_compare | 6 | 100% | 100% | - |
| underspecified_date | 6 | 50% | 50% | - |

## `azure:gpt-5.4-mini @ azure-gpt-5.4-mini-f67210` by intent

| intent | n | fully correct | exact route | early stop |
|---|---|---|---|---|
| ambiguous_date | 6 | 0% | 0% | - |
| compare_only | 6 | 100% | 100% | - |
| cross_account | 6 | 17% | 17% | - |
| date_with_no_data | 6 | 100% | 100% | 100% (6) |
| full_reconcile | 12 | 67% | 67% | 100% (6) |
| investigate_no_stage | 8 | 100% | 100% | 100% (3) |
| notes_check | 6 | 100% | 100% | - |
| out_of_scope_capability | 6 | 100% | 100% | - |
| out_of_scope_execute | 6 | 100% | 100% | - |
| premature_stage | 6 | 0% | 0% | - |
| rerun_compare | 6 | 100% | 100% | - |
| underspecified_date | 6 | 50% | 50% | - |

## Head to head

`azure:gpt-5.4-mini @ azure-gpt-5.4-mini-31daba` against `azure:gpt-5.4-mini @ azure-gpt-5.4-mini-2d0d89`, 80 paired requests.

- discordant pairs: 17 (azure:gpt-5.4-mini @ azure-gpt-5.4-mini-31daba only 12, azure:gpt-5.4-mini @ azure-gpt-5.4-mini-2d0d89 only 5)
- exact two-sided McNemar: p = 0.1435
- early-stop class (15 requests): azure:gpt-5.4-mini @ azure-gpt-5.4-mini-31daba 15, azure:gpt-5.4-mini @ azure-gpt-5.4-mini-2d0d89 15

