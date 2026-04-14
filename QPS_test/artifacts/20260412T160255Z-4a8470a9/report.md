# QPS Test Report: 20260412T160255Z-4a8470a9

## Overview
- Users: 120
- Target RPS: 120
- Total requests: 0
- Success: 0
- Failure: 0
- Blocked users: 0
- Actual average RPS: 0.00
- Actual peak RPS: 0
- Availability passed: False
- Log cap warning triggered: False
- Aborted due to log cap: False

## Availability Acceptance
- Normal chat success rate: 0.0000
- Transport failure rate: 0.0000
- Successful normal-chat user coverage: 0.0000
- Stop clean-completion rate: 1.0000
- Stop signal rate: 0.0000
- Failed checks: browser smoke login/stream failed, normal chat success rate below 99%, successful normal-chat user coverage below 99%

## Latency
- TTFT p50/p90/p95/p99 (ms): 0.00 / 0.00 / 0.00 / 0.00
- Wall p50/p90/p95/p99 (ms): 0.00 / 0.00 / 0.00 / 0.00
- Generation p50/p90/p95/p99 (ms): 0.00 / 0.00 / 0.00 / 0.00
- Output TPS p50/p90/p95/p99: 0.00 / 0.00 / 0.00 / 0.00

## Workload
- Session rollover count: 0
- Average turns per session: 0.00
- Natural completion rate: 0.0000
- Disturbance stop rate: 0.0000
- Post-stop next-turn success rate: 1.0000
- Stop success rate: 0.0000
- Thread switch success rate: 0.0000
- Average threads per user: 0.00
- Average successful requests before user limit: 0.00
- Average tokens before user limit: 0.00

## Browser Smoke
- Success: False
- Login: True
- Stream: False
- Stop: False
- Thread switch: False
- Notes: Locator.wait_for: Timeout 90000ms exceeded.
Call log:
  - waiting for locator("#stop-button") to be hidden
    184 × locator resolved to visible <button id="stop-button" data-state="closed" class="inline-flex items-center justify-center gap-2 whitespace-nowrap text-sm font-medium ring-offset-background transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:pointer-events-none disabled:opacity-50 [&_svg]:pointer-events-none [&_svg]:size-4 [&_svg]:shrink-0 bg-primary text-primary-foreground hover:bg-primary/90 rounded-full h-8 w-8">…</button>


## Artifacts
- requests.jsonl: `/home/ncoai/workspace/llmchat/ncoai/QPS_test/artifacts/20260412T160255Z-4a8470a9/requests.jsonl`
- failures.jsonl: `/home/ncoai/workspace/llmchat/ncoai/QPS_test/artifacts/20260412T160255Z-4a8470a9/failures.jsonl`
- latency.csv: `/home/ncoai/workspace/llmchat/ncoai/QPS_test/artifacts/20260412T160255Z-4a8470a9/latency.csv`
- summary.json: `/home/ncoai/workspace/llmchat/ncoai/QPS_test/artifacts/20260412T160255Z-4a8470a9/summary.json`
