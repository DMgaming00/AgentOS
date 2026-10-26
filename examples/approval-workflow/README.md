# Demo C: permission enforcement

`approval-workflow` workflow, two independent nodes:

* `draft_note`: the Writer Agent asks to write `approved-note.md`, which needs
  approval. Reject it: nothing is written, and the agent is told the action was not
  executed. It resubmits as a **new** approval request. Approve that one. Fault
  injection (`crash_after_tool: artifact_writer`) then kills the worker right after
  the write, before the step is recorded. The retry replays the succeeded execution
  by idempotency key, so exactly one artifact exists.
* `untrusted_attempt`: the Untrusted Agent (allowed: calculator) calls
  `artifact_writer`. The runner denies it by policy, and the denial is recorded.

Run: `make demo-permissions`, or decide the approvals yourself with
`uv run python scripts/demo.py permissions --interactive`.
