
### data/ops_requests.db   (modified 2026-10-13 10:54)

  run_id='e2e-b'  calls=232  sessions=45
    reconciliation   calculate_position_deltas        ok  48
    reconciliation   fetch_broker_statement           REJ 11
    reconciliation   fetch_broker_statement           ok  48
    reconciliation   fetch_internal_ledger            REJ 11
    reconciliation   fetch_internal_ledger            ok  48
    staging          stage_for_execution              ok  18
    triage           draft_client_inquiry             ok  24
    triage           flag_discrepancy                 ok  24
    REJECTED x2: unknown session: ACC-015-2026-09-29
    REJECTED x2: unknown session: ACC-015-2026-09-25
    REJECTED x2: unknown session: ACC-014-2026-09-25
    REJECTED x2: unknown session: ACC-011-2026-09-25
    REJECTED x2: unknown session: ACC-011-2026-09-10
    REJECTED x2: unknown session: ACC-010-2026-09-25
    REJECTED x2: unknown session: ACC-008-2026-09-03
    REJECTED x2: unknown session: ACC-006-2026-09-25
    REJECTED x2: unknown session: ACC-004-2026-09-29
    REJECTED x2: unknown session: ACC-004-2026-09-10
    REJECTED x2: unknown session: ACC-003-2026-09-03

  [single run -- HITL counts belong to it]
  pending_actions: {'PENDING_HUMAN': 14}  distinct discrepancies: 14
  discrepancies: {'flagged': 17}

### data/ops.db   (modified 2026-10-10 21:05)

  [single run -- HITL counts belong to it]
  pending_actions: {}  distinct discrepancies: 0
  discrepancies: {}

### data/ops_injected.db   (modified 2026-10-10 20:04)

  run_id='azure-gpt-5.4-nano@t0.0-bab8c6'  calls=336  sessions=60
    reconciliation   calculate_position_deltas        ok  60
    reconciliation   fetch_broker_statement           ok  60
    reconciliation   fetch_internal_ledger            ok  60
    staging          stage_for_execution              ok  52
    triage           draft_client_inquiry             ok  52
    triage           flag_discrepancy                 ok  52

  run_id='azure-gpt-5.4-nano@t0.0+notes-40a032'  calls=15  sessions=3
    reconciliation   calculate_position_deltas        ok  3
    reconciliation   fetch_broker_statement           ok  3
    reconciliation   fetch_internal_ledger            ok  3
    staging          stage_for_execution              ok  2
    triage           draft_client_inquiry             ok  2
    triage           flag_discrepancy                 ok  2

  run_id='azure-gpt-5.4-nano@t0.0+notes-a683e5'  calls=69  sessions=12
    reconciliation   calculate_position_deltas        ok  12
    reconciliation   fetch_broker_statement           ok  12
    reconciliation   fetch_internal_ledger            ok  12
    staging          stage_for_execution              ok  11
    triage           draft_client_inquiry             ok  11
    triage           flag_discrepancy                 ok  11

  [3 run(s) in this db -- HITL counts are whole-db]
  pending_actions: {'PENDING_HUMAN': 52}  distinct discrepancies: 52
  discrepancies: {'flagged': 52}
