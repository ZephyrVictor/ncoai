# QPS Test Report: 20260413T123052Z-f12b2e9c

## Overview
- Users: 80
- Target RPS: 120
- Total requests: 1089
- Success: 1052
- Failure: 37
- Blocked users: 0
- Actual average RPS: 8.20
- Actual peak RPS: 71
- Availability passed: False
- Log cap warning triggered: False
- Aborted due to log cap: False

## Availability Acceptance
- Normal chat success rate: 0.9663
- Transport failure rate: 0.0000
- Successful normal-chat user coverage: 0.9375
- Stop clean-completion rate: 1.0000
- Stop signal rate: 0.0000
- Failed checks: normal chat success rate below 99%, successful normal-chat user coverage below 99%

## Latency
- TTFT p50/p90/p95/p99 (ms): 0.00 / 0.00 / 0.00 / 0.00
- Wall p50/p90/p95/p99 (ms): 865.25 / 1373.41 / 1656.29 / 2223.14
- Generation p50/p90/p95/p99 (ms): 0.00 / 0.00 / 0.00 / 0.00
- Output TPS p50/p90/p95/p99: 0.00 / 0.00 / 0.00 / 0.00

## Workload
- Session rollover count: 32
- Average turns per session: 9.72
- Natural completion rate: 1.0000
- Disturbance stop rate: 0.0000
- Post-stop next-turn success rate: 1.0000
- Stop success rate: 0.9608
- Thread switch success rate: 0.0000
- Average threads per user: 1.40
- Average successful requests before user limit: 13.15
- Average tokens before user limit: 0.00

## Browser Smoke
- Skipped

## Artifacts
- requests.jsonl: `/home/ncoai/workspace/llmchat/ncoai/QPS_test/artifacts/20260413T123052Z-f12b2e9c/requests.jsonl`
- failures.jsonl: `/home/ncoai/workspace/llmchat/ncoai/QPS_test/artifacts/20260413T123052Z-f12b2e9c/failures.jsonl`
- latency.csv: `/home/ncoai/workspace/llmchat/ncoai/QPS_test/artifacts/20260413T123052Z-f12b2e9c/latency.csv`
- summary.json: `/home/ncoai/workspace/llmchat/ncoai/QPS_test/artifacts/20260413T123052Z-f12b2e9c/summary.json`
