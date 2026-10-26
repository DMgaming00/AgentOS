# Vector Forge

> Fictional sample document bundled with AgentOS for demos. All figures are invented.

## Overview

Vector Forge is a GPU cloud with a managed inference layer aimed at batch and offline workloads. It trades latency for low cost and offers spot-priced capacity.

## Pricing

- Price per 1M input tokens: $0.15 USD
- Price per 1M output tokens: $0.60 USD
- Dedicated GPU hour: $1.90 USD

Spot capacity can be preempted with 30 seconds notice.

## Performance

Vector Forge reports figures for an 8B-parameter model in its batch-optimized configuration.

- Median latency (p50): 260 ms
- p95 latency: 900 ms
- Throughput per GPU: 3,600 tokens/s
- Cold start time: 25 s

## Features

Vector Forge provides a batch job API, priority queues, 4-bit quantization and an OpenAI-compatible endpoint. Streaming is supported only on dedicated deployments.

## Limits and SLA

- Uptime SLA: 99.5 %
- Max context window: 64000 tokens
