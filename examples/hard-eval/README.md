# hard-ops: a harder evaluation set

150 deterministic cases (41 development / 50 validation / 59 test) over 12 **fictional** platforms (all figures invented).
Rebuild or check with `uv run python scripts/build_hard_eval.py [--check]`; `MANIFEST.json` holds SHA-256 hashes of every
document and dataset.

| Kind | What it needs |
|---|---|
| lookup | a single fact |
| compute_cost | two current prices and arithmetic (prices may be overridden by the changelog) |
| multi_hop | look up 4 platforms, filter on one metric, rank on another |
| conflict_latest | the pricing changelog overrides launch figures; some questions ask the value "as of" a date |
| ratio | two lookups and division |
| unanswerable | abstain; some platforms state a similar but different metric (a near miss) |
| buried | one fact in a long operations handbook |
| aggregate_count / aggregate_sum | exhaustive lookups over all 12 platforms with filters |
| cheapest_blend | argmin of total cost over 6 platforms with current prices; three platforms state prices **per 1K** tokens (a unit trap) |

Answer keys come from the in-memory fact model that also renders the documents, and the builder then re-parses the rendered
text with different code and fails if any fact, changelog event or handbook entry disagrees. Answers are graded
deterministically (numeric tolerance or case-insensitive exact match) with a tool-use check.

How hard it is depends on the agent: a Haiku agent with a calculator and 14 steps scored 92.7% on the development split, so the
real-model results use a 3-step-budget agent as the flawed baseline (see the README's Real-model results).

To use it: upload `documents/*.md` to a workspace and import `datasets/hard-ops.v2.*.json` (`scripts/real_hard_eval.py setup`).
