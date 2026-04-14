# QPS Test Report: 20260412T151814Z-620f0ac1

## Overview
- Users: 1
- Target RPS: 0
- Total requests: 13
- Success: 0
- Failure: 13
- Blocked users: 0
- Actual average RPS: 0.39
- Actual peak RPS: 1
- Availability passed: False
- Log cap warning triggered: False
- Aborted due to log cap: False

## Availability Acceptance
- Normal chat success rate: 0.0000
- Transport failure rate: 0.0000
- Successful normal-chat user coverage: 0.0000
- Stop clean-completion rate: 0.9231
- Stop signal rate: 0.0000
- Failed checks: normal chat success rate below 99%, successful normal-chat user coverage below 99%, stop clean-completion rate below 95%

## Latency
- TTFT p50/p90/p95/p99 (ms): 0.00 / 0.00 / 0.00 / 0.00
- Wall p50/p90/p95/p99 (ms): 0.00 / 0.00 / 0.00 / 0.00
- Generation p50/p90/p95/p99 (ms): 0.00 / 0.00 / 0.00 / 0.00
- Output TPS p50/p90/p95/p99: 0.00 / 0.00 / 0.00 / 0.00

## Workload
- Session rollover count: 1
- Average turns per session: 6.50
- Natural completion rate: 0.0000
- Disturbance stop rate: 0.0000
- Post-stop next-turn success rate: 1.0000
- Stop success rate: 0.0000
- Thread switch success rate: 0.0000
- Average threads per user: 2.00
- Average successful requests before user limit: 0.00
- Average tokens before user limit: 0.00

## Browser Smoke
- Skipped

## Artifacts
- requests.jsonl: `/home/ncoai/workspace/llmchat/ncoai/QPS_test/artifacts/20260412T151814Z-620f0ac1/requests.jsonl`
- failures.jsonl: `/home/ncoai/workspace/llmchat/ncoai/QPS_test/artifacts/20260412T151814Z-620f0ac1/failures.jsonl`
- latency.csv: `/home/ncoai/workspace/llmchat/ncoai/QPS_test/artifacts/20260412T151814Z-620f0ac1/latency.csv`
- summary.json: `/home/ncoai/workspace/llmchat/ncoai/QPS_test/artifacts/20260412T151814Z-620f0ac1/summary.json`
