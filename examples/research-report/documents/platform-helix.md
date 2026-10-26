# Helix Serve

> Fictional sample document bundled with AgentOS for demos. All figures are invented.

## Overview

Helix Serve is a self-hosted inference server sold with an enterprise support subscription. It runs on customer Kubernetes clusters and focuses on predictable latency for large models. The Helix overview page advertises a three-nines availability target.

- Uptime SLA: 99.9 %

## Pricing

Helix is licensed per GPU; token prices below are Helix's published estimates for typical utilization.

- Price per 1M input tokens: $0.22 USD
- Price per 1M output tokens: $0.95 USD
- Dedicated GPU hour: $3.40 USD

## Performance

Helix reports figures for an 8B-parameter model on its reference cluster.

- Median latency (p50): 140 ms
- p95 latency: 300 ms
- Throughput per GPU: 3,100 tokens/s

Helix does not publish cold start figures because deployments are always-on.

## Features

Helix supports tensor parallelism across up to 8 GPUs, paged attention, 8-bit quantization and a gRPC API. Autoscaling depends on the customer's cluster configuration.

## Limits and SLA

The enterprise contract specifies a higher availability commitment than the overview page.

- Uptime SLA: 99.95 %
- Max context window: 200000 tokens
