# QPS Test Report: 20260412T152523Z-241d09e3

## Overview
- Users: 1
- Target RPS: 0
- Total requests: 10
- Success: 0
- Failure: 10
- Blocked users: 0
- Actual average RPS: 0.49
- Actual peak RPS: 1
- Availability passed: False
- Log cap warning triggered: False
- Aborted due to log cap: False

## Availability Acceptance
- Normal chat success rate: 0.0000
- Transport failure rate: 0.0000
- Successful normal-chat user coverage: 0.0000
- Stop clean-completion rate: 1.0000
- Stop signal rate: 0.0000
- Failed checks: normal chat success rate below 99%, successful normal-chat user coverage below 99%

## Latency
- TTFT p50/p90/p95/p99 (ms): 0.00 / 0.00 / 0.00 / 0.00
- Wall p50/p90/p95/p99 (ms): 0.00 / 0.00 / 0.00 / 0.00
- Generation p50/p90/p95/p99 (ms): 0.00 / 0.00 / 0.00 / 0.00
- Output TPS p50/p90/p95/p99: 0.00 / 0.00 / 0.00 / 0.00

## Workload
- Session rollover count: 0
- Average turns per session: 10.00
- Natural completion rate: 0.0000
- Disturbance stop rate: 0.0000
- Post-stop next-turn success rate: 1.0000
- Stop success rate: 0.0000
- Thread switch success rate: 0.0000
- Average threads per user: 1.00
- Average successful requests before user limit: 0.00
- Average tokens before user limit: 0.00

## Browser Smoke
- Skipped

## Artifacts
- requests.jsonl: `/home/ncoai/workspace/llmchat/ncoai/QPS_test/artifacts/20260412T152523Z-241d09e3/requests.jsonl`
- failures.jsonl: `/home/ncoai/workspace/llmchat/ncoai/QPS_test/artifacts/20260412T152523Z-241d09e3/failures.jsonl`
- latency.csv: `/home/ncoai/workspace/llmchat/ncoai/QPS_test/artifacts/20260412T152523Z-241d09e3/latency.csv`
- summary.json: `/home/ncoai/workspace/llmchat/ncoai/QPS_test/artifacts/20260412T152523Z-241d09e3/summary.json`
