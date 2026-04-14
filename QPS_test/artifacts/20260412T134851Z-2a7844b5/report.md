# QPS Test Report: 20260412T134851Z-2a7844b5

## Overview
- Users: 10
- Target RPS: 5
- Total requests: 26
- Success: 26
- Failure: 0
- Blocked users: 0
- Actual average RPS: 0.61
- Actual peak RPS: 5
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
- TTFT p50/p90/p95/p99 (ms): 423.57 / 552.85 / 570.49 / 578.29
- Wall p50/p90/p95/p99 (ms): 12952.29 / 14268.62 / 14980.51 / 15459.91

## Workload
- Stop success rate: 0.0000
- Thread switch success rate: 0.0000
- Average threads per user: 1.00
- Average successful requests before user limit: 2.60
- Average tokens before user limit: 1368.40

## Browser Smoke
- Success: False
- Login: True
- Stream: True
- Stop: False
- Thread switch: False
- Screenshots: 01-first-thread.png
- Notes: Locator.wait_for: Timeout 5000ms exceeded.
Call log:
  - waiting for get_by_text("已停止") to be visible


## Artifacts
- requests.jsonl: `/home/ncoai/workspace/llmchat/ncoai/QPS_test/artifacts/20260412T134851Z-2a7844b5/requests.jsonl`
- failures.jsonl: `/home/ncoai/workspace/llmchat/ncoai/QPS_test/artifacts/20260412T134851Z-2a7844b5/failures.jsonl`
- latency.csv: `/home/ncoai/workspace/llmchat/ncoai/QPS_test/artifacts/20260412T134851Z-2a7844b5/latency.csv`
- summary.json: `/home/ncoai/workspace/llmchat/ncoai/QPS_test/artifacts/20260412T134851Z-2a7844b5/summary.json`
