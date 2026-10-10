# Evaluation

Scored against `data/seeded_truth.json`. Every number here is recomputable from `reports/trajectories.jsonl` with no model and no credentials.

## Human-in-the-loop invariant

52 staged action(s), all `PENDING_HUMAN`, none carrying a decider. **Holds.**

## Outcome and trajectory

| backend | recall | precision | wrong type | invented | clean-session FP | clean trajectory |
|---|---|---|---|---|---|---|
| `azure:gpt-5.4-nano@t0.0` | 100.0% (52/52) | 100.0% | 0 | 0 | 0/22 | 100.0% |

Recall and precision count `(trade, type)` pairs. *Wrong type* is a trade that was found and mislabelled; it is excluded from *invented*, which counts flags on trades where nothing differs. *Clean-session FP* is how many control sessions got a flag at all -- the number that an agent flagging everything cannot hide.

### `azure:gpt-5.4-nano@t0.0` by discrepancy type

| type | seeded | found | missed |
|---|---|---|---|
| quantity_mismatch | 12 | 12 | 0 |
| missing_trade | 10 | 10 | 0 |
| duplicate_booking | 13 | 13 | 0 |
| price_variance | 9 | 9 | 0 |
| settlement_date_shift | 8 | 8 | 0 |

Schema validity 100.0% over 336 tool call(s); 0 refusal(s).

