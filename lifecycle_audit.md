# IG Agent Adversarial Harmonization Lifecycle Audit

Initialized: 2026-06-22T21:41:02.974312+00:00

#### [PHASE 1/10] SYSTEM STATUS REPORT
- **Timestamp**: 2026-06-22T21:41:04.032313+00:00
- **Runtime Execution Status**: Success
- **Lightstreamer Ingestion Latency**: mean=6.51ms / max_spike=16.89ms
- **ML Inference Assessment**: tensor=n/a / confidence_spread=n/a / signal=n/a
- **Identified Trading Blockers**: none
- **Codebase Hardening Action Implemented**: reconnect_with_backoff on health API; harmonization/reconnect_policy.py
- **Lifecycle Close-Down Verification**: read-only probes — no positions opened

#### [PHASE 2/10] SYSTEM STATUS REPORT
- **Timestamp**: 2026-06-22T21:41:04.337028+00:00
- **Runtime Execution Status**: Success
- **Lightstreamer Ingestion Latency**: mean=n/ams / max_spike=n/ams
- **ML Inference Assessment**: tensor=n/a / confidence_spread=n/a / signal=n/a
- **Identified Trading Blockers**: none
- **Codebase Hardening Action Implemented**: feed hub hard reset restarts=0
- **Lifecycle Close-Down Verification**: buffers purged — no open orders

#### [PHASE 3/10] SYSTEM STATUS REPORT
- **Timestamp**: 2026-06-22T21:41:04.340045+00:00
- **Runtime Execution Status**: Success
- **Lightstreamer Ingestion Latency**: mean=n/ams / max_spike=n/ams
- **ML Inference Assessment**: tensor=n/a / confidence_spread=n/a / signal=n/a
- **Identified Trading Blockers**: none
- **Codebase Hardening Action Implemented**: cockpit_feed_guardian + pid_mismatch detection
- **Lifecycle Close-Down Verification**: SHM read-only

#### [PHASE 4/10] SYSTEM STATUS REPORT
- **Timestamp**: 2026-06-22T21:41:04.344480+00:00
- **Runtime Execution Status**: Success
- **Lightstreamer Ingestion Latency**: mean=0.0ms / max_spike=5000.0ms
- **ML Inference Assessment**: tensor=n/a / confidence_spread=n/a / signal=n/a
- **Identified Trading Blockers**: none
- **Codebase Hardening Action Implemented**: POST /api/cockpit/heal + agent_feed_guardian
- **Lifecycle Close-Down Verification**: feeds reset — flat

#### [PHASE 5/10] SYSTEM STATUS REPORT
- **Timestamp**: 2026-06-22T21:41:07.354619+00:00
- **Runtime Execution Status**: Success
- **Lightstreamer Ingestion Latency**: mean=n/ams / max_spike=n/ams
- **ML Inference Assessment**: tensor=n/a / confidence_spread=n/a / signal=n/a
- **Identified Trading Blockers**: none
- **Codebase Hardening Action Implemented**: exponential backoff 1/2/4/8/16s verified
- **Lifecycle Close-Down Verification**: recovered after 3 attempts

#### [PHASE 6/10] SYSTEM STATUS REPORT
- **Timestamp**: 2026-06-22T21:41:07.371213+00:00
- **Runtime Execution Status**: Success
- **Lightstreamer Ingestion Latency**: mean=n/ams / max_spike=n/ams
- **ML Inference Assessment**: tensor=volatility_gate / confidence_spread=52.5→47.92 / signal=dynamic floor applied
- **Identified Trading Blockers**:
  - CS.D.CFPGOLD.CFP.IP: execution: INTEGRITY_ABORT: missing gate_execution_params
  - CS.D.EURUSD.CFD.IP: ALPHA_MATRIX: miss (empty cell)
  - IX.D.DOW.IFM.IP: execution: INTEGRITY_ABORT: missing gate_execution_params
  - IX.D.NIKKEI.IFM.IP: ALPHA_MATRIX: miss (empty cell)
- **Codebase Hardening Action Implemented**: harmonization/volatility_gate.py dynamic_confidence_floor
- **Lifecycle Close-Down Verification**: audit only

#### [PHASE 7/10] SYSTEM STATUS REPORT
- **Timestamp**: 2026-06-22T21:41:22.880770+00:00
- **Runtime Execution Status**: Success
- **Lightstreamer Ingestion Latency**: mean=n/ams / max_spike=n/ams
- **ML Inference Assessment**: tensor=n/a / confidence_spread=n/a / signal=n/a
- **Identified Trading Blockers**:
  - CS.D.CFPGOLD.CFP.IP:  — execution: INTEGRITY_ABORT: missing gate_execution_params
  - CS.D.EURUSD.CFD.IP:  — ALPHA_MATRIX: miss (empty cell)
  - IX.D.DOW.IFM.IP:  — execution: INTEGRITY_ABORT: missing gate_execution_params
  - IX.D.NIKKEI.IFM.IP:  — ALPHA_MATRIX: miss (empty cell)
  - ledger shows 128 rows — verify deal_id present (phantom fill guard)
- **Codebase Hardening Action Implemented**: bare_metal shadow_force disabled for DEMO broker
- **Lifecycle Close-Down Verification**: probe read-only

#### [PHASE 8/10] SYSTEM STATUS REPORT
- **Timestamp**: 2026-06-22T21:41:22.881069+00:00
- **Runtime Execution Status**: Blocked
- **Lightstreamer Ingestion Latency**: mean=n/ams / max_spike=n/ams
- **ML Inference Assessment**: tensor=n/a / confidence_spread=n/a / signal=n/a
- **Identified Trading Blockers**:
  - REST auth failed: ImportError: cannot import name 'get_shared_rest_client' from 'ig_api.rest_client' (/Users/chrisgordon/Projects/IG_Agent_v25/src/ig_api/rest_client.py)
- **Codebase Hardening Action Implemented**: IronCladRiskEngine wired at LiveExecutor
- **Lifecycle Close-Down Verification**: no orders

#### [PHASE 9/10] SYSTEM STATUS REPORT
- **Timestamp**: 2026-06-22T21:41:22.881137+00:00
- **Runtime Execution Status**: Partial
- **Lightstreamer Ingestion Latency**: mean=n/ams / max_spike=n/ams
- **ML Inference Assessment**: tensor=n/a / confidence_spread=n/a / signal=n/a
- **Identified Trading Blockers**:
  - client unavailable: cannot import name 'get_shared_rest_client' from 'ig_api.rest_client' (/Users/chrisgordon/Projects/IG_Agent_v25/src/ig_api/rest_client.py)
- **Codebase Hardening Action Implemented**: night matrix epics validated against IG DEMO REST
- **Lifecycle Close-Down Verification**: no orders

#### [PHASE 10/10] SYSTEM STATUS REPORT
- **Timestamp**: 2026-06-22T21:41:45.528466+00:00
- **Runtime Execution Status**: Blocked
- **Lightstreamer Ingestion Latency**: mean=n/ams / max_spike=n/ams
- **ML Inference Assessment**: tensor=n/a / confidence_spread=n/a / signal=n/a
- **Identified Trading Blockers**:
  - LIVE fire blocked — set IG_ALLOW_LIVE_FIRE=1 + LIVE_PROMOTION_CHECKLIST for real funds. Running DEMO routing validation only.
  - e2e failed
- **Codebase Hardening Action Implemented**: Phase 10 DEMO e2e — live requires explicit operator gate
- **Lifecycle Close-Down Verification**: no live positions — DEMO routing check only

