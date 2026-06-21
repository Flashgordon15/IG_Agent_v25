# CHAOS COMPLIANCE & PENETRATION REPORT

Generated: 2026-06-21T15:43:01.376502+00:00
Host PID: 82749

## Executive Summary

- Timeline events captured: **1**
- Live runtime registry: `/tmp/ig_agent_parallel.pids.json`

## Granular Forensic Timeline

| UTC Timestamp | Phase | Status | Latency (µs) | Event |
|---|---|---|---:|---|
| 2026-06-21T15:43:01.366767+00:00 | NET-CLEANUP-CONTRACT | PASS | 473335 | Network chaos cleanup contract (exit0 + lock unlink + SHM clear) |

## Verdict Matrix

- **PASS events:** 1
- **FAIL / FATAL events:** 0
- **Overall chaos compliance:** CLEAN

## Runtime Guard Tail (last hits)

```
2026-06-21 16:42:53 | runtime_guard:network_failure_teardown source=LiveExecutor._execute_order_blocking ConnectionError ConnectionError: drop
2026-06-21 16:42:53 | runtime_guard:network_failure_teardown source=LiveExecutor._execute_order_blocking ConnectionError ConnectionError: drop
2026-06-21 16:43:01 | runtime_guard:network_failure_teardown source=LiveExecutor._execute_order_blocking ConnectionError ConnectionError: drop
2026-06-21 16:43:01 | runtime_guard:network_failure_teardown source=LiveExecutor._execute_order_blocking ConnectionError ConnectionError: drop
```
