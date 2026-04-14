# QPS Test Report: 20260412T151528Z-7fa52bda

## Overview
- Users: 1
- Target RPS: 0
- Total requests: 4
- Success: 4
- Failure: 0
- Blocked users: 0
- Actual average RPS: 0.04
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
- TTFT p50/p90/p95/p99 (ms): 432.81 / 637.64 / 675.19 / 705.22
- Wall p50/p90/p95/p99 (ms): 22979.15 / 29930.64 / 30146.08 / 30318.43
- Generation p50/p90/p95/p99 (ms): 22389.99 / 29433.42 / 29695.34 / 29904.88
- Output TPS p50/p90/p95/p99: 13.98 / 14.35 / 14.42 / 14.47

## Workload
- Session rollover count: 1
- Average turns per session: 2.00
- Natural completion rate: 1.0000
- Disturbance stop rate: 0.0000
- Post-stop next-turn success rate: 1.0000
- Stop success rate: 0.0000
- Thread switch success rate: 0.0000
- Average threads per user: 2.00
- Average successful requests before user limit: 4.00
- Average tokens before user limit: 3246.00

## Browser Smoke
- Skipped

## Artifacts
- requests.jsonl: `/home/ncoai/workspace/llmchat/ncoai/QPS_test/artifacts/20260412T151528Z-7fa52bda/requests.jsonl`
- failures.jsonl: `/home/ncoai/workspace/llmchat/ncoai/QPS_test/artifacts/20260412T151528Z-7fa52bda/failures.jsonl`
- latency.csv: `/home/ncoai/workspace/llmchat/ncoai/QPS_test/artifacts/20260412T151528Z-7fa52bda/latency.csv`
- summary.json: `/home/ncoai/workspace/llmchat/ncoai/QPS_test/artifacts/20260412T151528Z-7fa52bda/summary.json`
