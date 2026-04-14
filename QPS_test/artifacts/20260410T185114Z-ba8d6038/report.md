# QPS Test Report: 20260410T185114Z-ba8d6038

## Overview
- Users: 1
- Target RPS: 1
- Total requests: 2
- Success: 2
- Failure: 0
- Blocked users: 0
- Actual average RPS: 0.07
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
- TTFT p50/p90/p95/p99 (ms): 307.38 / 308.47 / 308.61 / 308.72
- Wall p50/p90/p95/p99 (ms): 13945.19 / 14533.67 / 14607.23 / 14666.07

## Workload
- Stop success rate: 0.0000
- Thread switch success rate: 0.0000
- Average threads per user: 1.00
- Average successful requests before user limit: 2.00
- Average tokens before user limit: 863.00

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
- requests.jsonl: `/home/ncoai/workspace/llmchat/ncoai/QPS_test/artifacts/20260410T185114Z-ba8d6038/requests.jsonl`
- failures.jsonl: `/home/ncoai/workspace/llmchat/ncoai/QPS_test/artifacts/20260410T185114Z-ba8d6038/failures.jsonl`
- latency.csv: `/home/ncoai/workspace/llmchat/ncoai/QPS_test/artifacts/20260410T185114Z-ba8d6038/latency.csv`
- summary.json: `/home/ncoai/workspace/llmchat/ncoai/QPS_test/artifacts/20260410T185114Z-ba8d6038/summary.json`
