# Demo A: multi-agent research report

`research-report` workflow (seeded by `python -m agentos.seed`; definition in
`packages/shared/agentos_core/demo.py`):

```
research_nimbus ─┐
research_helix ──┼─> analyze ──> write_report ──(approval)──> inference-platform-comparison.md
research_vectorforge ─┘
```

* `documents/` holds three **fictional** platform documents, a CSV of benchmark
  figures (read by Demo B), and `forum-notes.md`, which contains a prompt-injection
  attempt used by the security tests.
* Deliberate evidence problems the analysis should surface: Helix Serve's uptime SLA
  conflicts between two sections (99.9% vs 99.95%), and Helix publishes no cold-start
  time.
* Expected report sections: Summary, Method, Numerical comparison (table with best
  value, mean and max/min ratio from the calculator tool), Observations
  (source-backed, with `document#section` citations), Conclusions (labeled as
  interpretation), Missing or conflicting evidence, References.

Run: `make demo-research`, or Workflows -> research-report -> Start run in the UI.
With `ANTHROPIC_API_KEY` set, seed with `AGENTOS_DEMO_PROVIDER=anthropic` to run the
same agents on Claude.
