# QPS Test Report: 20260412T150810Z-143b94cb

## Overview
- Users: 1
- Target RPS: 0
- Total requests: 1
- Success: 1
- Failure: 0
- Blocked users: 0
- Actual average RPS: 0.01
- Actual peak RPS: 1
- Availability passed: True
- Log cap warning triggered: False
- Aborted due to log cap: False

## Availability Acceptance
- Normal chat success rate: 1.0000
- Transport failure rate: 0.0000
- Successful normal-chat user coverage: 1.0000
- Stop clean-completion rate: 1.0000
- Stop signal rate: 0.0000
- Failed checks: none

## Latency
- TTFT p50/p90/p95/p99 (ms): 96438.72 / 96438.72 / 96438.72 / 96438.72
- Wall p50/p90/p95/p99 (ms): 124990.08 / 124990.08 / 124990.08 / 124990.08
- Generation p50/p90/p95/p99 (ms): 28550.19 / 28550.19 / 28550.19 / 28550.19
- Output TPS p50/p90/p95/p99: 13.80 / 13.80 / 13.80 / 13.80

## Workload
- Session rollover count: 0
- Average turns per session: 1.00
- Natural completion rate: 1.0000
- Disturbance stop rate: 0.0000
- Post-stop next-turn success rate: 1.0000
- Stop success rate: 0.0000
- Thread switch success rate: 0.0000
- Average threads per user: 1.00
- Average successful requests before user limit: 1.00
- Average tokens before user limit: 613.00

## Browser Smoke
- Skipped

## Artifacts
- requests.jsonl: `/home/ncoai/workspace/llmchat/ncoai/QPS_test/artifacts/20260412T150810Z-143b94cb/requests.jsonl`
- failures.jsonl: `/home/ncoai/workspace/llmchat/ncoai/QPS_test/artifacts/20260412T150810Z-143b94cb/failures.jsonl`
- latency.csv: `/home/ncoai/workspace/llmchat/ncoai/QPS_test/artifacts/20260412T150810Z-143b94cb/latency.csv`
- summary.json: `/home/ncoai/workspace/llmchat/ncoai/QPS_test/artifacts/20260412T150810Z-143b94cb/summary.json`
