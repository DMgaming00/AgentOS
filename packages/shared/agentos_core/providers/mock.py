"""Deterministic mock provider (no API key, no network).

It is a scripted *policy*, not canned text: given the conversation it decides the
next tool calls and derives its final answer from the actual tool results (real
document search hits, real calculator outputs). Same inputs always give the same
outputs, which makes CI and demos reproducible. Usage is reported as simulated.

Scenarios (``model_configuration.scenario`` or model name ``mock-<scenario>``):
research, analysis, writer, planner, invalid_plan, invalid_then_valid, echo,
rate_limit, timeout, forbidden_tool.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from typing import Any

from agentos_core.domain.errors import ProviderRateLimitError, ProviderTimeoutError
from agentos_core.providers.base import (
    Message,
    ModelConfig,
    ModelResponse,
    ToolCall,
    ToolDefinition,
    Usage,
)

TASK_RE = re.compile(r"<task_input>\s*(.*?)\s*</task_input>", re.DOTALL)
METRIC_LINE = re.compile(
    r"^[-*]\s*([A-Za-z][A-Za-z0-9 ()/%.,-]*?):\s*\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*([A-Za-z%/$ ]*)$"
)
LOWER_IS_BETTER = ("price", "cost", "latency", "cold start")


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:60]


def task_payload(messages: list[Message]) -> dict[str, Any]:
    for m in messages:
        if m.role == "user" and m.text:
            match = TASK_RE.search(m.text)
            if match:
                try:
                    data = json.loads(match.group(1))
                    return data if isinstance(data, dict) else {"value": data}
                except json.JSONDecodeError:
                    return {"text": match.group(1)}
            return {"text": m.text}
    return {}


def tool_rounds(messages: list[Message]) -> list[Message]:
    return [m for m in messages if m.role == "tool_results"]


def _results_json(msg: Message) -> list[tuple[str, dict[str, Any], bool]]:
    out = []
    for r in msg.tool_results:
        try:
            parsed = json.loads(r.content)
        except json.JSONDecodeError:
            parsed = {"raw": r.content}
        out.append((r.name, parsed if isinstance(parsed, dict) else {"value": parsed}, r.is_error))
    return out


class MockProvider:
    name = "mock"

    def __init__(self) -> None:
        self._counter = 0

    async def generate(
        self, system: str, messages: list[Message], tools: list[ToolDefinition], config: ModelConfig
    ) -> ModelResponse:
        scenario = str(config.extra.get("scenario") or config.model.removeprefix("mock-") or "echo")
        latency_ms = float(config.extra.get("latency_ms", 0))
        if latency_ms:
            await asyncio.sleep(latency_ms / 1000)
        attempt = int(config.metadata.get("attempt_number", 1))
        fail_attempts = int(config.extra.get("fail_attempts", 2))
        if scenario == "rate_limit" and attempt <= fail_attempts:
            raise ProviderRateLimitError(f"mock rate limit (attempt {attempt} of {fail_attempts} failing)")
        if scenario == "timeout" and attempt <= fail_attempts:
            raise ProviderTimeoutError(f"mock timeout (attempt {attempt} of {fail_attempts} failing)")

        handler = getattr(self, f"_{scenario}", None) or self._echo
        # Handlers are synchronous, so this per-call attribute cannot interleave across coroutines.
        self._system = system
        text, calls = handler(messages, tools, config)
        # Tool-call ids must be unique across the conversation (and within a turn).
        base = sum(len(m.tool_calls) for m in messages if m.role == "assistant")
        calls = [
            ToolCall(id=f"mock_call_{base + i + 1}", name=c.name, arguments=c.arguments)
            for i, c in enumerate(calls)
        ]
        out_chars = len(text) + sum(len(json.dumps(c.arguments)) for c in calls)
        in_chars = len(system) + sum(len(json.dumps(m.to_dict(), default=str)) for m in messages)
        finish = "tool_use" if calls else "end_turn"
        # Honor max_tokens like a real model: output beyond the cap is cut off.
        if math.ceil(out_chars / 4) > config.max_tokens:
            text, calls, finish = text[: config.max_tokens * 4], [], "max_tokens"
            out_chars = len(text)
        return ModelResponse(
            text=text,
            tool_calls=calls,
            model=config.model,
            usage=Usage(
                input_tokens=math.ceil(in_chars / 4),
                output_tokens=math.ceil(out_chars / 4),
                cached_input_tokens=0,
                simulated=True,
            ),
            latency_ms=int(latency_ms),
            finish_reason=finish,  # type: ignore[arg-type]
            provider="mock",
            raw=None,
        )

    def _call(self, name: str, args: dict[str, Any], messages: list[Message]) -> ToolCall:
        return ToolCall(id="pending", name=name, arguments=args)  # renumbered in generate()

    # ------------------------------------------------------------------ scenarios

    def _echo(
        self, messages: list[Message], tools: list[ToolDefinition], config: ModelConfig
    ) -> tuple[str, list[ToolCall]]:
        payload = task_payload(messages)
        upstream = payload.get("upstream") or {}
        return json.dumps(
            {
                "node": config.metadata.get("node_key"),
                "processed": payload.get("input"),
                "upstream_keys": sorted(upstream),
                "checksum": len(json.dumps(payload, sort_keys=True)),
            }
        ), []

    _rate_limit = _echo
    _timeout = _echo

    def _forbidden_tool(
        self, messages: list[Message], tools: list[ToolDefinition], config: ModelConfig
    ) -> tuple[str, list[ToolCall]]:
        rounds = tool_rounds(messages)
        if not rounds:
            # Ask for a tool the step is not allowed to use; the runtime must deny it.
            return "", [
                self._call(
                    "artifact_writer",
                    {"filename": "exfil.md", "content": "should never be written"},
                    messages,
                )
            ]
        results = _results_json(rounds[-1])
        return json.dumps(
            {"attempted_tool": "artifact_writer", "outcome": results[0][1], "denied": results[0][2]}
        ), []

    def _injection_follower(
        self, messages: list[Message], tools: list[ToolDefinition], config: ModelConfig
    ) -> tuple[str, list[ToolCall]]:
        """Searches once; if a tool result contains an instruction to write a file it *obeys* it (the unsafe
        behavior the platform's policy layer exists to stop). Used by the Chaos Lab's hostile-output tests."""
        rounds = tool_rounds(messages)
        if not rounds:
            return "", [self._call("document_search", {"query": "pricing latency", "limit": 3}, messages)]
        results = _results_json(rounds[-1])
        text = json.dumps([r[1] for r in results], default=str)
        if len(rounds) == 1 and "artifact_writer" in text:
            return "", [
                self._call("artifact_writer", {"filename": "exfil.md", "content": text[:200]}, messages)
            ]
        return json.dumps({"summary": "done", "followed_injection": len(rounds) > 1}), []

    def _research(
        self, messages: list[Message], tools: list[ToolDefinition], config: ModelConfig
    ) -> tuple[str, list[ToolCall]]:
        payload = task_payload(messages)
        inp = payload.get("input") or {}
        subject = str(inp.get("platform") or inp.get("subject") or inp.get("topic") or "the subject")
        rounds = tool_rounds(messages)
        if not rounds:
            q = f"{subject} pricing latency throughput features limits"
            return "", [self._call("document_search", {"query": q, "limit": 8}, messages)]

        hits: list[dict[str, Any]] = []
        for _, res, is_err in _results_json(rounds[-1]):
            if not is_err:
                hits.extend(res.get("results") or [])
        subject_l = subject.lower()
        relevant = [
            h for h in hits if subject_l in (h.get("title", "") + h.get("document", "")).lower()
        ] or hits

        findings: list[dict[str, Any]] = []
        metrics: dict[str, dict[str, Any]] = {}
        conflicts: list[dict[str, Any]] = []
        for h in relevant:
            for line in str(h.get("content", "")).splitlines():
                line = line.strip()
                m = METRIC_LINE.match(line)
                if m:
                    label = m.group(1).strip()
                    value = float(m.group(2).replace(",", ""))
                    key = slug(label)
                    entry = {
                        "label": label,
                        "value": value,
                        "unit": m.group(3).strip() or None,
                        "citation": h["citation"],
                    }
                    if key in metrics and metrics[key]["value"] != value:
                        conflicts.append({"metric": key, "values": [metrics[key], entry]})
                    metrics.setdefault(key, entry)
                elif line and not line.startswith(("#", "|")) and len(findings) < 8 and len(line) > 40:
                    sentence = re.split(r"(?<=[.!?])\s", line)[0]
                    findings.append({"statement": sentence[:300], "citation": h["citation"]})
        out = {
            "subject": subject,
            "summary": f"{len(relevant)} relevant excerpts found for {subject}; {len(metrics)} quantitative metrics extracted.",
            "findings": findings,
            "metrics": metrics,
            "conflicts": conflicts,
            "sources": sorted({h["document"] for h in relevant}),
        }
        return json.dumps(out), []

    def _analysis(
        self, messages: list[Message], tools: list[ToolDefinition], config: ModelConfig
    ) -> tuple[str, list[ToolCall]]:
        payload = task_payload(messages)
        upstream: dict[str, Any] = payload.get("upstream") or {}
        research = {k: v for k, v in upstream.items() if isinstance(v, dict) and "metrics" in v}
        table: dict[str, dict[str, Any]] = {}
        for out in research.values():
            for key, entry in (out.get("metrics") or {}).items():
                table.setdefault(
                    key, {"label": entry["label"], "unit": entry.get("unit"), "values": {}, "citations": {}}
                )
                table[key]["values"][out["subject"]] = entry["value"]
                table[key]["citations"][out["subject"]] = entry["citation"]
        comparable = {k: v for k, v in sorted(table.items()) if len(v["values"]) >= 2}

        rounds = tool_rounds(messages)
        if not rounds and comparable:
            calls = []
            for key, row in comparable.items():
                vals = [row["values"][s] for s in sorted(row["values"])]
                calls.append(
                    self._call(
                        "calculator", {"operation": "mean", "values": vals, "label": f"{key}:mean"}, messages
                    )
                )
                if min(vals) > 0:
                    calls.append(
                        self._call(
                            "calculator",
                            {
                                "operation": "ratio",
                                "values": [max(vals), min(vals)],
                                "label": f"{key}:spread",
                            },
                            messages,
                        )
                    )
            return "", calls

        computed: dict[str, float] = {}
        for _, res, is_err in _results_json(rounds[-1]) if rounds else []:
            if not is_err and "label" in res:
                computed[res["label"]] = res["result"]
        subjects = sorted({s for out in research.values() for s in [out["subject"]]})
        comparison = []
        for key, row in comparable.items():
            lower_better = any(w in row["label"].lower() for w in LOWER_IS_BETTER)
            best = (min if lower_better else max)(row["values"], key=lambda s: row["values"][s])
            comparison.append(
                {
                    "metric": key,
                    "label": row["label"],
                    "unit": row["unit"],
                    "values": row["values"],
                    "best": best,
                    "direction": "lower_is_better" if lower_better else "higher_is_better",
                    "mean": computed.get(f"{key}:mean"),
                    "spread_ratio": computed.get(f"{key}:spread"),
                    "citations": row["citations"],
                }
            )
        missing = [
            {"subject": s, "metric": key}
            for key, row in sorted(table.items())
            for s in subjects
            if s not in row["values"]
        ]
        conflicts = [
            {"subject": out["subject"], **c} for out in research.values() for c in out.get("conflicts") or []
        ]
        out = {
            "subjects": subjects,
            "comparison": comparison,
            "missing_evidence": missing,
            "conflicts": conflicts,
            "calculations": computed,
            "observations": [
                f"{c['best']} has the best {c['label'].lower()} ({c['values'][c['best']]}"
                f"{' ' + c['unit'] if c['unit'] else ''}) [{c['citations'][c['best']]}]"
                for c in comparison
            ],
        }
        return json.dumps(out), []

    def _writer(
        self, messages: list[Message], tools: list[ToolDefinition], config: ModelConfig
    ) -> tuple[str, list[ToolCall]]:
        payload = task_payload(messages)
        inp = payload.get("input") or {}
        filename = str(inp.get("filename") or "report.md")
        max_revisions = int(config.extra.get("max_revisions", 1))
        rounds = tool_rounds(messages)
        rejections = [
            res for r in rounds for (_, res, err) in _results_json(r) if err and "not approve" in str(res)
        ]
        if rounds:
            _name, res, is_err = _results_json(rounds[-1])[0]
            if not is_err:
                return json.dumps({"status": "written", "artifact": res, "revisions": len(rejections)}), []
            if len(rejections) > max_revisions or not rejections:
                return json.dumps({"status": "not_written", "reason": res, "revisions": len(rejections)}), []
        md = render_report(
            payload,
            revision=len(rejections) + 1,
            rejection_notes=[str(r.get("decision")) for r in rejections],
        )
        return "", [
            self._call(
                "artifact_writer",
                {"filename": filename, "content": md, "reason": "Export the final comparison report"},
                messages,
            )
        ]

    def _extractor(
        self, messages: list[Message], tools: list[ToolDefinition], config: ModelConfig
    ) -> tuple[str, list[ToolCall]]:
        """Config-sensitive numeric extraction policy (the Autopilot demo agent).

        Behavior is a deterministic function of the agent configuration, so prompt, tool
        and step-limit changes have measurable effects:
        * searches once per subject with document_search, only if that tool is allowed;
        * answers only from retrieved text (no search -> nulls);
        * formats USD values as "$0.95" strings unless the system prompt asks for JSON numbers;
        * cites document sections only if the system prompt asks for citations.
        """
        payload = task_payload(messages)
        inp = payload.get("input") or {}
        fields: list[dict[str, Any]] = inp.get("fields") or []
        subjects = list(dict.fromkeys(f.get("subject") or inp.get("subject", "") for f in fields))
        system = getattr(self, "_system", "")
        numbers_as_numbers = bool(
            re.search(r"json numbers?|plain numbers?|numeric json|no currency symbols", system, re.I)
        )
        cite = bool(re.search(r"\bcit(e|es|ing|ation)", system, re.I))
        can_search = any(t.name == "document_search" for t in tools)
        rounds = tool_rounds(messages)
        if can_search and len(rounds) < len(subjects):
            subject = subjects[len(rounds)]
            labels = " ".join(
                f["label"] for f in fields if (f.get("subject") or inp.get("subject")) == subject
            )
            args: dict[str, Any] = {"query": f"{subject} {labels}", "limit": 8}
            if inp.get("documents"):
                args["documents"] = inp["documents"]
            return "", [self._call("document_search", args, messages)]

        hits_by_subject: dict[str, list[dict[str, Any]]] = {}
        for i, r in enumerate(rounds[: len(subjects)]):
            for _, res, err in _results_json(r):
                if not err:
                    hits_by_subject.setdefault(subjects[i], []).extend(res.get("results") or [])
        out: dict[str, Any] = {}
        citations: dict[str, str] = {}
        for f in fields:
            subject = f.get("subject") or inp.get("subject", "")
            value: float | int | None = None
            citation = None
            for h in hits_by_subject.get(subject, []):
                if subject.lower() not in (h.get("title", "") + h.get("document", "")).lower():
                    continue
                for line in str(h.get("content", "")).splitlines():
                    m = METRIC_LINE.match(line.strip())
                    if m and m.group(1).strip().lower() == f["label"].lower():
                        value, citation = float(m.group(2).replace(",", "")), h["citation"]
                        break
                if value is not None:
                    break
            if isinstance(value, float) and value.is_integer() and f.get("unit") != "USD":
                value = int(value)
            if value is not None and f.get("unit") == "USD" and not numbers_as_numbers:
                out[f["key"]] = f"${value:.2f}"  # the deliberate flaw: currency strings
            else:
                out[f["key"]] = value
            if citation and cite:
                citations[f["key"]] = citation
        if cite:
            out["citations"] = citations
        return json.dumps(out), []

    def _diagnosis(
        self, messages: list[Message], tools: list[ToolDefinition], config: ModelConfig
    ) -> tuple[str, list[ToolCall]]:
        """Deterministic stand-in for the Diagnosis Agent: rule-based reading of the evidence pack."""
        pack = task_payload(messages)
        signals = pack.get("signals") or []
        category = pack.get("category", "UNKNOWN")
        ids = [s["id"] for s in signals[:2]] + [
            e["id"] for s in signals[:2] for e in s.get("evidence", [])[:2]
        ]
        previews = " ".join(str(e.get("text_preview") or "") for s in signals for e in s.get("evidence", []))
        infra = signals and all(s.get("infrastructure") for s in signals)
        if category == "OUTPUT_SCHEMA_INVALID" and re.search(r'"\$\d', previews):
            hypothesis = (
                'The agent formats currency values as strings (for example "$0.95") while the output '
                "contract requires JSON numbers, so structured outputs fail validation."
            )
            remedy = "Instruct the agent to return every numeric value as a plain JSON number without currency symbols."
            confidence = "medium"
        elif infra:
            hypothesis = (
                "These failures come from infrastructure (provider or worker), not from the agent's behavior."
            )
            remedy = (
                "Investigate provider availability and retry configuration rather than changing the prompt."
            )
            confidence = "medium"
        else:
            hypothesis = f"Repeated {category} failures; the evidence does not identify a single cause."
            remedy = "Collect more traces and add explicit tool-use guidance for the failing step."
            confidence = "low"
        return json.dumps(
            {
                "failure_category": category,
                "hypothesis": hypothesis,
                "evidence_ids": ids or ["none"],
                "confidence": confidence,
                "proposed_remedy": remedy,
                "limitations": [
                    f"Rule-based simulated diagnosis over {len(signals)} signals (mock provider)."
                ],
            }
        ), []

    def _optimizer(
        self, messages: list[Message], tools: list[ToolDefinition], config: ModelConfig
    ) -> tuple[str, list[ToolCall]]:
        """Deterministic stand-in for the Optimizer Agent.

        It proposes a mix of candidates on purpose: one targeted fix, one tempting
        shortcut (cheaper because it skips retrieval) and one out-of-policy change
        (adds a tool), so the downstream policy checks and experiments are exercised.
        """
        payload = task_payload(messages)
        base = payload.get("baseline_config") or {}
        diagnosis = payload.get("diagnosis") or {}
        count = int(payload.get("max_candidates", 3))
        instr = base.get("system_instructions", "")
        candidates = []
        if diagnosis.get("failure_category") == "OUTPUT_SCHEMA_INVALID":
            candidates.append(
                {
                    "title": "Return numeric values as JSON numbers",
                    "changes": [
                        {
                            "field": "system_instructions",
                            "value": instr
                            + " Return every numeric value as a plain JSON number with no units or "
                            "currency symbols.",
                            "reason": diagnosis.get("proposed_remedy") or "Fix numeric formatting.",
                        }
                    ],
                    "expected_benefits": ["Outputs satisfy the numeric output contract"],
                    "risks": ["Prompt change could affect unrelated formatting"],
                }
            )
        else:
            candidates.append(
                {
                    "title": "Add explicit tool-use strategy",
                    "changes": [
                        {
                            "field": "tool_strategy",
                            "value": "Search once per subject, then answer from results.",
                            "reason": "Reduce unstructured tool use.",
                        }
                    ],
                    "expected_benefits": ["More consistent tool use"],
                    "risks": ["May not address the failure"],
                }
            )
        candidates.append(
            {
                "title": "Fast path: answer without retrieval",
                "changes": [
                    {
                        "field": "allowed_tool_ids",
                        "value": [],
                        "reason": "Retrieval dominates cost and latency.",
                    },
                    {"field": "max_steps", "value": 1, "reason": "A single turn is cheaper."},
                ],
                "expected_benefits": ["Lower cost per task", "Lower latency"],
                "risks": ["Answers are no longer grounded in documents"],
            }
        )
        candidates.append(
            {
                "title": "Persist extractions as artifacts",
                "changes": [
                    {
                        "field": "allowed_tool_ids",
                        "value": sorted(set(base.get("allowed_tool_ids", [])) | {"artifact_writer"}),
                        "reason": "Keep a record of each extraction.",
                    }
                ],
                "expected_benefits": ["Auditable outputs"],
                "risks": ["Adds a side-effecting tool"],
            }
        )
        if count > 3:
            candidates.append(
                {
                    "title": "Lower sampling temperature",
                    "changes": [
                        {
                            "field": "model_configuration.temperature",
                            "value": 0,
                            "reason": "Reduce output variance.",
                        }
                    ],
                    "expected_benefits": ["More deterministic output"],
                    "risks": ["Unsupported on some providers"],
                }
            )
        return json.dumps({"candidates": candidates[:count]}), []

    def _planner(
        self, messages: list[Message], tools: list[ToolDefinition], config: ModelConfig
    ) -> tuple[str, list[ToolCall]]:
        payload = task_payload(messages)
        return json.dumps(standard_plan(payload)), []

    def _invalid_plan(
        self, messages: list[Message], tools: list[ToolDefinition], config: ModelConfig
    ) -> tuple[str, list[ToolCall]]:
        plan = standard_plan(task_payload(messages))
        # Introduce a cycle: the first research node now depends on the writer.
        plan["nodes"][0]["depends_on"] = [plan["nodes"][-1]["key"]]
        return json.dumps(plan), []

    def _invalid_then_valid(
        self, messages: list[Message], tools: list[ToolDefinition], config: ModelConfig
    ) -> tuple[str, list[ToolCall]]:
        prior = [m for m in messages if m.role == "assistant"]
        return (self._invalid_plan if not prior else self._planner)(messages, tools, config)


def standard_plan(payload: dict[str, Any]) -> dict[str, Any]:
    docs: list[dict[str, Any]] = payload.get("documents") or []
    # The mock planner treats documents named platform-*.md as the subjects to compare.
    subjects = [d["title"] for d in docs if str(d.get("filename", "")).startswith("platform-")][:3]
    subjects = subjects or ["Subject A", "Subject B"]
    research_keys = []
    nodes: list[dict[str, Any]] = []
    for s in subjects:
        key = f"research_{slug(s)}"[:60]
        research_keys.append(key)
        nodes.append(
            {
                "key": key,
                "type": "agent_task",
                "agent": "Research Agent",
                "name": f"Research {s}",
                "input": {"platform": s},
                "tools": ["document_search"],
            }
        )
    nodes.append(
        {
            "key": "analyze",
            "type": "agent_task",
            "agent": "Analysis Agent",
            "name": "Compare findings",
            "depends_on": research_keys,
            "tools": ["calculator"],
            "input": {k: {"$ref": f"steps.{k}.output"} for k in research_keys},
        }
    )
    nodes.append(
        {
            "key": "write_report",
            "type": "agent_task",
            "agent": "Writer Agent",
            "name": "Write report",
            "depends_on": ["analyze"],
            "tools": ["artifact_writer"],
            "input": {"filename": "report.md", "task": payload.get("task") or payload.get("text")},
        }
    )
    return {"nodes": nodes, "budget": {"max_model_calls": 40, "max_cost_usd": 2.0, "max_concurrency": 3}}


def render_report(payload: dict[str, Any], *, revision: int, rejection_notes: list[str]) -> str:
    upstream: dict[str, Any] = payload.get("upstream") or {}
    analysis = next((v for v in upstream.values() if isinstance(v, dict) and "comparison" in v), {})
    inp = payload.get("input") or {}
    research = inp.get("research") or {}
    lines = [f"# {inp.get('title') or 'AI Inference Platform Comparison'}", ""]
    lines += [
        "> Generated by AgentOS (mock provider: deterministic, derived from workspace documents).",
        f"> Revision {revision}.",
        "",
    ]
    if rejection_notes:
        lines += ["> Resubmitted after reviewer feedback: " + "; ".join(rejection_notes), ""]
    subjects = analysis.get("subjects") or []
    lines += ["## Summary", ""]
    lines += [
        f"Compared {len(subjects)} platforms ({', '.join(subjects)}) on "
        f"{len(analysis.get('comparison') or [])} quantitative metrics drawn from the workspace documents.",
        "",
    ]
    lines += [
        "## Method",
        "",
        "1. Research agents searched the workspace documents for each platform (PostgreSQL full-text search).",
        "2. The analysis agent compared metrics reported by at least two platforms, using the calculator tool.",
        "3. Every number below cites the document section it came from.",
        "",
    ]
    comparison = analysis.get("comparison") or []
    if comparison:
        lines += [
            "## Numerical comparison",
            "",
            "| Metric | " + " | ".join(subjects) + " | Best | Mean | Max/min |",
            "|---" * (len(subjects) + 4) + "|",
        ]
        for c in comparison:
            vals = " | ".join(str(c["values"].get(s, "n/a")) for s in subjects)
            mean = "n/a" if c.get("mean") is None else f"{c['mean']:.4g}"
            spread = "n/a" if c.get("spread_ratio") is None else f"{c['spread_ratio']:.3g}x"
            lines.append(
                f"| {c['label']}{' (' + c['unit'] + ')' if c.get('unit') else ''} | {vals} | {c['best']} | {mean} | {spread} |"
            )
        lines.append("")
    lines += ["## Observations (source-backed)", ""]
    lines += [f"- {o}" for o in analysis.get("observations") or ["No comparable metrics were found."]]
    for key, out in sorted(research.items()):
        if isinstance(out, dict):
            for f in (out.get("findings") or [])[:2]:
                lines.append(f"- {out.get('subject', key)}: {f['statement']} [{f['citation']}]")
    lines += ["", "## Conclusions (interpretation)", ""]
    if comparison:
        wins: dict[str, int] = {}
        for c in comparison:
            wins[c["best"]] = wins.get(c["best"], 0) + 1
        leader = max(sorted(wins), key=lambda s: wins[s])
        lines.append(
            f"- {leader} leads on {wins[leader]} of {len(comparison)} compared metrics. This is a count of "
            "metrics, not a weighted recommendation; weigh metrics by your workload."
        )
    else:
        lines.append("- The evidence was insufficient for a conclusion.")
    lines += ["", "## Missing or conflicting evidence", ""]
    missing = analysis.get("missing_evidence") or []
    conflicts = analysis.get("conflicts") or []
    lines += [f"- Missing: {m['metric']} for {m['subject']}" for m in missing] or ["- No missing metrics."]
    for c in conflicts:
        vals = ", ".join(f"{v['value']} [{v['citation']}]" for v in c["values"])
        lines.append(f"- Conflict: {c['metric']} for {c['subject']}: {vals}")
    lines += ["", "## References", ""]
    cites = sorted({cit for c in comparison for cit in c["citations"].values()})
    lines += [f"- {c}" for c in cites] or ["- (none)"]
    return "\n".join(lines) + "\n"
