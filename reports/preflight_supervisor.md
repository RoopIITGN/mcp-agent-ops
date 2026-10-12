# Supervisor pre-flight

- deployment: `gpt-5.4-nano`
- temperature: rejected, running at the default
- token parameter: `max_completion_tokens`
- reasoning_effort: low
- tokens per decision: 1,513

| check | result | detail |
|---|---|---|
| a forced submit_decision call parses | pass | first step: invoke  reconciliation  2026-09-08  --  Run reconciliation for ACC-003 as of 2026-09-08 (8 September) to identify all field-level  |
| the first step is reconciliation, as it must be | pass |  |
| the date resolved to ISO | pass |  |
| an out-of-scope request is answerable | pass | out of scope: clarify  --  Need to resolve the as-of date and determine whether any reconciliation/discrepancies have |
| it refuses what no role can do | FAIL | it did not refuse an execute request; SYSTEM_CANNOT is in the prompt but not being used |
| reasoning_effort is accepted | pass | with reasoning: invoke  reconciliation  2026-09-08  --  User requested reconciliation for ACC-003 on 8 September (resolved to 2026-09-08 based on  |
| an out-of-scope request is answerable with reasoning | pass | out of scope, with reasoning: invoke  reconciliation  2026-09-08  --  Must first run reconciliation for ACC-003 as-of 2026-09-08 to identify and repor |
| it refuses what no role can do, with reasoning | FAIL | neither arm refuses an execute request with the rule stated; that is a finding, not a prompt bug |

Temperature cannot be pinned on this deployment, so the routing arms are measured at its default sampling. Any difference smaller than the variance floor is not reportable, and the floor is measured by running one arm twice.
