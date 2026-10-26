# OpenEval

Evaluate whether agents *correctly complete tasks*, not whether an LLM thinks
their answers sound good. OpenEval has no dependency on AgentOS: it grades
`Observation`s (output, tool calls, retrieved sources, usage) against versioned
`Dataset`s.

* **Deterministic first.** `schema_validation`, `exact_match`, `numeric_tolerance`,
  `tool_correctness`, `source_citation_check` and `task_completion` are pure
  functions. `llm_judge` is *advisory*: it is recorded with its uncertainty and never
  decides pass/fail when deterministic checks exist.
* **Versioned, hashed datasets** with explicit splits (development, validation,
  test). Hidden splits are never given to optimizers.
* **Honest metrics.** Every rate carries its denominator. Cost per successful task is
  `None` when there are no successes, never 0.
* **Paired comparisons** use a seeded bootstrap CI and an exact McNemar test.
  `significant` is only reported when the CI excludes 0 *and* the sample-size floor is
  met.

```python
from openeval import Dataset, Observation, grade_case, aggregate, compare
```
