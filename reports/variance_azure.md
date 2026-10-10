# Variance floor

Two runs of `azure:gpt-5.4-nano@t0.0` over the same sessions, same configuration, nothing changed between them. Anything smaller than this is noise, not a result.

**60 paired session(s). 60 identical in findings, staging and trajectory — a flip rate of 0.0%.**

| measure | sessions that disagreed | rate |
|---|---|---|
| findings, as (trade, type) | 0 | 0.0% |
| corrections staged | 0 | 0.0% |
| trajectory clean | 0 | 0.0% |

No session disagreed with itself. At this sample size the floor is below 1 in 60, which is the most that can be said -- a zero observed in 60 trials is not a zero rate. It does mean every difference reported elsewhere in these results is larger than the noise this measurement could detect.

