"""Scripted policies for the Agent Arena (mock scenario ``arena``).

One configurable policy handles the three Arena task families (``input.family``). As with the Autopilot extractor, its
behavior is a deterministic function of the *agent configuration*, so different configurations score differently for
reasons that can be read off the traces:

* **tools**: without ``calculator`` it does "mental arithmetic" (the exact answer rounded to 3 significant digits);
  without ``document_search`` it cannot ground an answer and reports null;
* **system prompt**: asking for JSON numbers (no currency strings), citations, and abstention ("never guess; return
  null when not stated") switches the corresponding behavior on;
* **max_steps** bounds how many search rounds a multi-subject question can afford.

These are scripted stand-ins for model behavior. A leaderboard built from them measures the platform's evaluation
machinery and the effect of configuration, not the intelligence of any real model.
"""

from __future__ import annotations

import json
import math
import re
from typing import TYPE_CHECKING, Any

from agentos_core.providers.base import Message, ModelConfig, ToolCall, ToolDefinition

if TYPE_CHECKING:
    from agentos_core.providers.mock import MockProvider

Handler = tuple[str, list[ToolCall]]
LOWER_IS_BETTER = ("price", "cost", "latency", "cold start")
LINE = re.compile(
    r"^[-*]\s*([A-Za-z][A-Za-z0-9 ()/%.,-]*?):\s*\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*([A-Za-z%/$ ]*)$"
)


def flags(system: str) -> dict[str, bool]:
    return {
        "numbers": bool(
            re.search(r"json numbers?|plain numbers?|numeric json|no currency symbols", system, re.I)
        ),
        "cite": bool(re.search(r"\bcit(e|es|ing|ation)", system, re.I)),
        "abstain": bool(
            re.search(r"never guess|do not guess|return null|answer null|unknown when", system, re.I)
        ),
    }


def significant(value: float, digits: int = 3) -> float:
    if value == 0 or not math.isfinite(value):
        return value
    return round(value, digits - 1 - math.floor(math.log10(abs(value))))


def arena(
    provider: MockProvider, messages: list[Message], tools: list[ToolDefinition], config: ModelConfig
) -> Handler:
    from agentos_core.providers.mock import task_payload

    inp: dict[str, Any] = task_payload(messages).get("input") or {}
    family = inp.get("family")
    if family == "extraction":
        return provider._extractor(messages, tools, config)
    if family == "calculation":
        return _calculation(provider, messages, tools, inp)
    if family == "grounded_qa":
        return _grounded_qa(provider, messages, tools, inp)
    return json.dumps({"error": f"unknown arena family {family!r}"}), []


def _calculation(
    provider: MockProvider, messages: list[Message], tools: list[ToolDefinition], inp: dict[str, Any]
) -> Handler:
    from agentos_core.providers.mock import _results_json, tool_rounds

    args: dict[str, Any] = {"operation": inp["operation"], "label": "arena"}
    if inp["operation"] == "evaluate":
        args["expression"] = inp["expression"]
    else:
        args["values"] = inp["values"]
    rounds = tool_rounds(messages)
    if any(t.name == "calculator" for t in tools):
        if not rounds:
            return "", [provider._call("calculator", args, messages)]
        _, res, is_err = _results_json(rounds[-1])[0]
        return json.dumps({"result": None if is_err else res.get("result")}), []
    # No calculator: do it "in the head" - right to three significant digits, which is wrong for most inputs.
    from agentos_core.tools.calculator import CalculatorArgs, compute

    try:
        return json.dumps({"result": significant(compute(CalculatorArgs.model_validate(args)))}), []
    except Exception:
        return json.dumps({"result": None}), []


def _hits(round_: Message) -> list[dict[str, Any]]:
    from agentos_core.providers.mock import _results_json

    out: list[dict[str, Any]] = []
    for _, res, is_err in _results_json(round_):
        if not is_err:
            out.extend(res.get("results") or [])
    return out


def _metrics(hits: list[dict[str, Any]], subject: str | None) -> list[tuple[str, float, str]]:
    """(label, value, citation) lines from hits whose document or title names the subject (all hits if None)."""
    found: list[tuple[str, float, str]] = []
    for h in hits:
        if subject and subject.lower() not in (h.get("title", "") + h.get("document", "")).lower():
            continue
        for line in str(h.get("content", "")).splitlines():
            m = LINE.match(line.strip())
            if m:
                found.append((m.group(1).strip(), float(m.group(2).replace(",", "")), h["citation"]))
    return found


def _grounded_qa(
    provider: MockProvider, messages: list[Message], tools: list[ToolDefinition], inp: dict[str, Any]
) -> Handler:
    from agentos_core.providers.mock import tool_rounds

    f = flags(getattr(provider, "_system", ""))
    kind = inp.get("kind")
    label = str(inp.get("label", ""))
    subject = inp.get("subject")
    rounds = tool_rounds(messages)
    can_search = any(t.name == "document_search" for t in tools)

    def answer(value: Any, cite: str | None) -> Handler:
        out: dict[str, Any] = {"answer": value}
        if f["cite"] and cite and value is not None:
            out["citations"] = {"answer": cite}
        return json.dumps(out), []

    if not can_search:
        return answer(None, None)
    if not rounds:
        query = f"{subject} {label}" if kind != "comparison" else label
        return "", [provider._call("document_search", {"query": query, "limit": 8}, messages)]
    hits = _hits(rounds[-1])

    if kind == "comparison":
        by_doc: dict[str, tuple[float, str, str]] = {}
        for h in hits:
            for lab, value, cite in _metrics([h], None):
                if lab.lower() == label.lower():
                    by_doc.setdefault(str(h.get("document", "")), (value, cite, str(h.get("title", ""))))
        if not by_doc:
            return answer(None, None)
        lower = inp.get("direction") == "lower_is_better" or any(w in label.lower() for w in LOWER_IS_BETTER)
        best = (min if lower else max)(by_doc.values(), key=lambda t: t[0])
        return answer(best[2], best[1])

    lines = _metrics(hits, str(subject))
    for lab, value, cite in lines:
        if lab.lower() == label.lower():
            return answer(int(value) if value.is_integer() else value, cite)
    if f["abstain"] or not lines:
        return answer(None, None)
    # Overconfident: no matching line, so report the closest-looking one (shared word) or the first number found.
    words = set(re.findall(r"[a-z0-9]+", label.lower()))
    scored = sorted(lines, key=lambda t: -len(words & set(re.findall(r"[a-z0-9]+", t[0].lower()))))
    lab, value, cite = scored[0]
    return answer(int(value) if value.is_integer() else value, cite)
