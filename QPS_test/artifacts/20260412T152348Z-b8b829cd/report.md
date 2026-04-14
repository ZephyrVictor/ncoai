# QPS Test Report: 20260412T152348Z-b8b829cd

## Overview
- Users: 1
- Target RPS: 0
- Total requests: 3
- Success: 3
- Failure: 0
- Blocked users: 0
- Actual average RPS: 0.14
- Actual peak RPS: 1
- Availability passed: False
- Log cap warning triggered: False
- Aborted due to log cap: False

## Availability Acceptance
- Normal chat success rate: 0.0000
- Transport failure rate: 0.0000
- Successful normal-chat user coverage: 0.0000
- Stop clean-completion rate: 1.0000
- Stop signal rate: 1.0000
- Failed checks: normal chat success rate below 99%, successful normal-chat user coverage below 99%

## Latency
- TTFT p50/p90/p95/p99 (ms): 394.31 / 637.19 / 667.55 / 691.84
- Wall p50/p90/p95/p99 (ms): 5953.90 / 6199.97 / 6230.73 / 6255.34
- Generation p50/p90/p95/p99 (ms): 5558.70 / 5561.38 / 5561.72 / 5561.98
- Output TPS p50/p90/p95/p99: 14.50 / 14.70 / 14.73 / 14.75

## Workload
- Session rollover count: 0
- Average turns per session: 3.00
- Natural completion rate: 0.0000
- Disturbance stop rate: 1.0000
- Post-stop next-turn success rate: 1.0000
- Stop success rate: 1.0000
- Thread switch success rate: 0.0000
- Average threads per user: 1.00
- Average successful requests before user limit: 3.00
- Average tokens before user limit: 1732.00

## Browser Smoke
- Skipped

## Artifacts
- requests.jsonl: `/home/ncoai/workspace/llmchat/ncoai/QPS_test/artifacts/20260412T152348Z-b8b829cd/requests.jsonl`
- failures.jsonl: `/home/ncoai/workspace/llmchat/ncoai/QPS_test/artifacts/20260412T152348Z-b8b829cd/failures.jsonl`
- latency.csv: `/home/ncoai/workspace/llmchat/ncoai/QPS_test/artifacts/20260412T152348Z-b8b829cd/latency.csv`
- summary.json: `/home/ncoai/workspace/llmchat/ncoai/QPS_test/artifacts/20260412T152348Z-b8b829cd/summary.json`
