# Nimbus Inference

> Fictional sample document bundled with AgentOS for demos. All figures are invented.

## Overview

Nimbus Inference is a managed serverless platform for hosting open-weight language models. It targets teams that want pay-per-token pricing without managing GPU clusters. Nimbus supports continuous batching and speculative decoding on all hosted models.

## Pricing

Nimbus bills per token with no minimum commitment. Dedicated deployments are billed per GPU hour.

- Price per 1M input tokens: $0.30 USD
- Price per 1M output tokens: $1.20 USD
- Dedicated GPU hour: $2.90 USD

## Performance

Figures below were reported by Nimbus for an 8B-parameter model at 512 concurrent requests.

- Median latency (p50): 180 ms
- p95 latency: 420 ms
- Throughput per GPU: 2,400 tokens/s
- Cold start time: 9 s

## Features

Nimbus provides autoscaling to zero, an OpenAI-compatible HTTP API, 4-bit and 8-bit quantization, and per-request token streaming. Fine-tuned adapters (LoRA) can be hot-swapped without redeploying.

## Limits and SLA

- Uptime SLA: 99.9 %
- Max context window: 128000 tokens

Rate limits default to 600 requests per minute per project and can be raised on request.
