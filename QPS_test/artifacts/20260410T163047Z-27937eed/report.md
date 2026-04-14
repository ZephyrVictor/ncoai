# QPS Test Report: 20260410T163047Z-27937eed

## Overview
- Users: 120
- Target RPS: 20
- Total requests: 3445
- Success: 3337
- Failure: 108
- Blocked users: 0
- Actual average RPS: 10.86
- Actual peak RPS: 35
- Availability passed: False
- Log cap warning triggered: False
- Aborted due to log cap: False

## Availability Acceptance
- Normal chat success rate: 0.9680
- Transport failure rate: 0.0122
- Successful normal-chat user coverage: 1.0000
- Stop clean-completion rate: 0.9812
- Stop signal rate: 0.1062
- Failed checks: normal chat success rate below 99%, transport failure rate above 1%

## Latency
- TTFT p50/p90/p95/p99 (ms): 9445.26 / 24259.35 / 24875.84 / 27977.26
- Wall p50/p90/p95/p99 (ms): 3065.33 / 5294.12 / 28436.19 / 66743.06

## Workload
- Stop success rate: 0.9812
- Thread switch success rate: 0.0000
- Average threads per user: 1.43
- Average successful requests before user limit: 27.81
- Average tokens before user limit: 1115.35

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
- requests.jsonl: `/home/ncoai/workspace/llmchat/ncoai/QPS_test/artifacts/20260410T163047Z-27937eed/requests.jsonl`
- failures.jsonl: `/home/ncoai/workspace/llmchat/ncoai/QPS_test/artifacts/20260410T163047Z-27937eed/failures.jsonl`
- latency.csv: `/home/ncoai/workspace/llmchat/ncoai/QPS_test/artifacts/20260410T163047Z-27937eed/latency.csv`
- summary.json: `/home/ncoai/workspace/llmchat/ncoai/QPS_test/artifacts/20260410T163047Z-27937eed/summary.json`
