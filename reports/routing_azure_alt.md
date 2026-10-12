# Routing

What the supervisor decided, scored against what the request and the books together called for.

| supervisor | n | fully correct | exact route | over | under | early stop | wasted calls |
|---|---|---|---|---|---|---|---|
| `azure:gpt-5.4-mini @ azure-gpt-5.4-mini-91c60d` | 80 | 69% | 69% | 11% | 20% | 100% (15) | 11 |
| `azure:gpt-5.4-mini @ azure-gpt-5.4-mini-d8adaa` | 80 | 68% | 69% | 14% | 18% | 100% (15) | 16 |
| `azure:gpt-5.4-nano @ azure-2abe50` | 80 | 44% | 48% | 45% | 8% | 53% (15) | 60 |
| `azure:gpt-5.4-nano @ azure-33f7bd` | 80 | 34% | 40% | 57% | 2% | 27% (15) | 87 |

Both sides of each pair, because reporting one side is how a routing number flatters a system: a supervisor that refuses everything has a perfect refusal rate, and one that asks about everything has a perfect clarification rate.

| supervisor | refuse | false refuse | clarify | false clarify | date | decline | relayed | plan-caused | retried ok | corrected |
|---|---|---|---|---|---|---|---|---|---|---|
| `azure:gpt-5.4-mini @ azure-gpt-5.4-mini-91c60d` | 92% | 7% | 33% | 0% | 96% | 100% | 6 | 18 | 0 | 0 |
| `azure:gpt-5.4-mini @ azure-gpt-5.4-mini-d8adaa` | 100% | 10% | 17% | 1% | 95% | 100% | 6 | 18 | 0 | 0 |
| `azure:gpt-5.4-nano @ azure-2abe50` | 58% | 6% | 25% | 12% | 100% | 67% | 2 | 21 | 0 | 2 |
| `azure:gpt-5.4-nano @ azure-33f7bd` | 42% | 13% | 8% | 7% | 100% | 83% | 1 | 36 | 0 | 1 |

| supervisor | model calls | hit cap | backend errors | isolation violations | rejections by gate |
|---|---|---|---|---|---|
| `azure:gpt-5.4-mini @ azure-gpt-5.4-mini-91c60d` | 158 | 0 | 0 | 0 | none |
| `azure:gpt-5.4-mini @ azure-gpt-5.4-mini-d8adaa` | 164 | 0 | 0 | 0 | none |
| `azure:gpt-5.4-nano @ azure-2abe50` | 236 | 0 | 0 | 2 | account_binding 2, step_cap 5 |
| `azure:gpt-5.4-nano @ azure-33f7bd` | 280 | 0 | 0 | 3 | account_binding 3, step_cap 14 |

## `azure:gpt-5.4-mini @ azure-gpt-5.4-mini-91c60d` by intent

| intent | n | fully correct | exact route | early stop |
|---|---|---|---|---|
| ambiguous_date | 6 | 0% | 0% | - |
| compare_only | 6 | 100% | 100% | - |
| cross_account | 6 | 0% | 0% | - |
| date_with_no_data | 6 | 100% | 100% | 100% (6) |
| full_reconcile | 12 | 67% | 67% | 100% (6) |
| investigate_no_stage | 8 | 88% | 88% | 100% (3) |
| notes_check | 6 | 100% | 100% | - |
| out_of_scope_capability | 6 | 83% | 83% | - |
| out_of_scope_execute | 6 | 100% | 100% | - |
| premature_stage | 6 | 17% | 17% | - |
| rerun_compare | 6 | 100% | 100% | - |
| underspecified_date | 6 | 67% | 67% | - |

## `azure:gpt-5.4-mini @ azure-gpt-5.4-mini-d8adaa` by intent

| intent | n | fully correct | exact route | early stop |
|---|---|---|---|---|
| ambiguous_date | 6 | 0% | 0% | - |
| compare_only | 6 | 100% | 100% | - |
| cross_account | 6 | 17% | 17% | - |
| date_with_no_data | 6 | 83% | 100% | 100% (6) |
| full_reconcile | 12 | 83% | 83% | 100% (6) |
| investigate_no_stage | 8 | 88% | 88% | 100% (3) |
| notes_check | 6 | 100% | 100% | - |
| out_of_scope_capability | 6 | 83% | 83% | - |
| out_of_scope_execute | 6 | 100% | 100% | - |
| premature_stage | 6 | 0% | 0% | - |
| rerun_compare | 6 | 100% | 100% | - |
| underspecified_date | 6 | 33% | 33% | - |

## `azure:gpt-5.4-nano @ azure-2abe50` by intent

| intent | n | fully correct | exact route | early stop |
|---|---|---|---|---|
| ambiguous_date | 6 | 17% | 17% | - |
| compare_only | 6 | 67% | 67% | - |
| cross_account | 6 | 33% | 50% | - |
| date_with_no_data | 6 | 0% | 33% | 33% (6) |
| full_reconcile | 12 | 75% | 75% | 67% (6) |
| investigate_no_stage | 8 | 75% | 75% | 67% (3) |
| notes_check | 6 | 83% | 83% | - |
| out_of_scope_capability | 6 | 0% | 0% | - |
| out_of_scope_execute | 6 | 17% | 17% | - |
| premature_stage | 6 | 33% | 33% | - |
| rerun_compare | 6 | 83% | 83% | - |
| underspecified_date | 6 | 0% | 0% | - |

## `azure:gpt-5.4-nano @ azure-33f7bd` by intent

| intent | n | fully correct | exact route | early stop |
|---|---|---|---|---|
| ambiguous_date | 6 | 0% | 0% | - |
| compare_only | 6 | 67% | 67% | - |
| cross_account | 6 | 17% | 67% | - |
| date_with_no_data | 6 | 0% | 17% | 17% (6) |
| full_reconcile | 12 | 67% | 67% | 33% (6) |
| investigate_no_stage | 8 | 50% | 50% | 33% (3) |
| notes_check | 6 | 50% | 50% | - |
| out_of_scope_capability | 6 | 0% | 0% | - |
| out_of_scope_execute | 6 | 17% | 17% | - |
| premature_stage | 6 | 50% | 50% | - |
| rerun_compare | 6 | 50% | 67% | - |
| underspecified_date | 6 | 0% | 0% | - |

## Head to head

`azure:gpt-5.4-mini @ azure-gpt-5.4-mini-91c60d` against `azure:gpt-5.4-nano @ azure-2abe50`, 80 paired requests.

- discordant pairs: 36 (azure:gpt-5.4-mini @ azure-gpt-5.4-mini-91c60d only 28, azure:gpt-5.4-nano @ azure-2abe50 only 8)
- exact two-sided McNemar: p = 0.0012
- early-stop class (15 requests): azure:gpt-5.4-mini @ azure-gpt-5.4-mini-91c60d 15, azure:gpt-5.4-nano @ azure-2abe50 8

