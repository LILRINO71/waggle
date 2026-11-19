# Waggle benchmark

_2026-11-18 17:05 · 40 labelled requests · local model `waggle-4b` · run with `python -m waggle.bench`_

## Router (local half: heuristics + local sorter)

_The study/code/robot escalation rule was added after an earlier run missed 2 of these 40, so this set is not a fair test of it; the held-out set at the bottom is._

| Metric | Result |
|---|---|
| Routing accuracy | **100%** |
| Simple requests kept local | 15 |
| Hard requests escalated to Claude | 25 |
| Missed escalations (hard request answered locally) | 0 |
| Wasted escalations (simple request sent to Claude) | 0 |
| Share handled locally for free | 38% |
| Specialist pick accuracy | 95% |
| Routing latency p50 / p95 | 2.57 s / 2.68 s |

## Real usage (event log)

17 messages routed.

| Route | Count | Median latency |
|---|---|---|
| local | 14 | 3.0 s |
| sonnet | 3 | 11.4 s |

Second-opinion reviews: 0 · screen looks: 2 · calls placed: 0

## Held-out set (not used for tuning)

_2026-11-18 17:06 · 20 labelled requests · local model `waggle-4b` · run with `python -m waggle.bench`_

## Router (local half: heuristics + local sorter)

| Metric | Result |
|---|---|
| Routing accuracy | **100%** |
| Simple requests kept local | 8 |
| Hard requests escalated to Claude | 12 |
| Missed escalations (hard request answered locally) | 0 |
| Wasted escalations (simple request sent to Claude) | 0 |
| Share handled locally for free | 40% |
| Specialist pick accuracy | 70% |
| Routing latency p50 / p95 | 2.56 s / 2.65 s |
