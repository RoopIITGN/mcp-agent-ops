# Routing

What the supervisor decided, scored against what the request and the books together called for.

| supervisor | n | fully correct | exact route | over | under | early stop | wasted calls |
|---|---|---|---|---|---|---|---|
| `stub:course_corrects` | 80 | 0% | 41% | 30% | 29% | 100% (15) | 24 |
| `stub:fixed` | 80 | 15% | 22% | 78% | 0% | 0% (15) | 143 |
| `stub:obeys_literally` | 80 | 66% | 74% | 19% | 8% | 0% (15) | 27 |
| `stub:oracle` | 80 | 100% | 100% | 0% | 0% | 100% (15) | 0 |

Both sides of each pair, because reporting one side is how a routing number flatters a system: a supervisor that refuses everything has a perfect refusal rate, and one that asks about everything has a perfect clarification rate.

| supervisor | refuse | false refuse | clarify | false clarify | date | decline | relayed | plan-caused | retried ok | corrected |
|---|---|---|---|---|---|---|---|---|---|---|
| `stub:course_corrects` | 0% | 0% | 100% | 100% | 0% | 0% | 6 | 222 | 0 | 12 |
| `stub:fixed` | 0% | 0% | 0% | 0% | 100% | 0% | 0 | 36 | 0 | 0 |
| `stub:obeys_literally` | 100% | 0% | 100% | 0% | 100% | 0% | 0 | 0 | 0 | 0 |
| `stub:oracle` | 100% | 0% | 100% | 0% | 100% | 100% | 6 | 0 | 0 | 0 |

| supervisor | model calls | hit cap | backend errors | isolation violations | rejections by gate |
|---|---|---|---|---|---|
| `stub:course_corrects` | 160 | 0 | 0 | 0 | none |
| `stub:fixed` | 320 | 0 | 0 | 0 | none |
| `stub:obeys_literally` | 198 | 0 | 0 | 0 | none |
| `stub:oracle` | 177 | 0 | 0 | 0 | none |

## `stub:course_corrects` by intent

| intent | n | fully correct | exact route | early stop |
|---|---|---|---|---|
| ambiguous_date | 6 | 0% | 0% | - |
| compare_only | 6 | 0% | 100% | - |
| cross_account | 6 | 0% | 0% | - |
| date_with_no_data | 6 | 0% | 100% | 100% (6) |
| full_reconcile | 12 | 0% | 50% | 100% (6) |
| investigate_no_stage | 8 | 0% | 38% | 100% (3) |
| notes_check | 6 | 0% | 100% | - |
| out_of_scope_capability | 6 | 0% | 0% | - |
| out_of_scope_execute | 6 | 0% | 0% | - |
| premature_stage | 6 | 0% | 0% | - |
| rerun_compare | 6 | 0% | 100% | - |
| underspecified_date | 6 | 0% | 0% | - |

## `stub:fixed` by intent

| intent | n | fully correct | exact route | early stop |
|---|---|---|---|---|
| ambiguous_date | 6 | 0% | 0% | - |
| compare_only | 6 | 0% | 0% | - |
| cross_account | 6 | 0% | 100% | - |
| date_with_no_data | 6 | 0% | 0% | 0% (6) |
| full_reconcile | 12 | 50% | 50% | 0% (6) |
| investigate_no_stage | 8 | 0% | 0% | 0% (3) |
| notes_check | 6 | 0% | 0% | - |
| out_of_scope_capability | 6 | 0% | 0% | - |
| out_of_scope_execute | 6 | 0% | 0% | - |
| premature_stage | 6 | 100% | 100% | - |
| rerun_compare | 6 | 0% | 0% | - |
| underspecified_date | 6 | 0% | 0% | - |

## `stub:obeys_literally` by intent

| intent | n | fully correct | exact route | early stop |
|---|---|---|---|---|
| ambiguous_date | 6 | 100% | 100% | - |
| compare_only | 6 | 100% | 100% | - |
| cross_account | 6 | 0% | 100% | - |
| date_with_no_data | 6 | 0% | 0% | 0% (6) |
| full_reconcile | 12 | 50% | 50% | 0% (6) |
| investigate_no_stage | 8 | 62% | 62% | 0% (3) |
| notes_check | 6 | 100% | 100% | - |
| out_of_scope_capability | 6 | 100% | 100% | - |
| out_of_scope_execute | 6 | 100% | 100% | - |
| premature_stage | 6 | 0% | 0% | - |
| rerun_compare | 6 | 100% | 100% | - |
| underspecified_date | 6 | 100% | 100% | - |

## `stub:oracle` by intent

| intent | n | fully correct | exact route | early stop |
|---|---|---|---|---|
| ambiguous_date | 6 | 100% | 100% | - |
| compare_only | 6 | 100% | 100% | - |
| cross_account | 6 | 100% | 100% | - |
| date_with_no_data | 6 | 100% | 100% | 100% (6) |
| full_reconcile | 12 | 100% | 100% | 100% (6) |
| investigate_no_stage | 8 | 100% | 100% | 100% (3) |
| notes_check | 6 | 100% | 100% | - |
| out_of_scope_capability | 6 | 100% | 100% | - |
| out_of_scope_execute | 6 | 100% | 100% | - |
| premature_stage | 6 | 100% | 100% | - |
| rerun_compare | 6 | 100% | 100% | - |
| underspecified_date | 6 | 100% | 100% | - |

## Head to head

`stub:oracle` against `stub:fixed`, 80 paired requests.

- discordant pairs: 68 (stub:oracle only 68, stub:fixed only 0)
- exact two-sided McNemar: p = 0.0000
- early-stop class (15 requests): stub:oracle 15, stub:fixed 0

The early-stop split is the one worth reading. A supervisor that commits to a route before any worker has read anything cannot win those requests whatever its reasoning budget: at the moment it commits, nobody has looked at the books. That is an information-ordering difference, not a prompt-quality one.

