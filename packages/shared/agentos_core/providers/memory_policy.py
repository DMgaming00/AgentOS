"""Scripted question-answering policy for the Memory Lab (mock scenario ``memory_qa``).

The agent answers *only* from what it is shown: earlier turns of the conversation (short-term context) and a list of
memory snippets (untrusted context). It has no tools and no knowledge of the scenario. Its matching rules are fixed
and deliberately ordinary, so differences between strategies come from what each strategy puts in front of it:

* statements look like ``<Entity> <attribute>: <value>`` (records), ``<Entity> <attribute> is <value>`` (conversation) or
  ``Summary for <Entity>: owner X; region Y; deadline Z`` (summaries);
* a statement applies to a question when its attribute matches and its entity *name appears inside the question's entity
  name* (so a statement about "Atlas" also matches a question about "Atlas Pro": ordinary sloppy matching, and the source of
  "incorrect memory reuse"); the longest entity match wins, ties go to the earliest snippet;
* conversation turns take precedence over memory snippets (the most recent information wins);
* with no applicable statement the answer is ``unknown``.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from agentos_core.providers.base import Message, ModelConfig, ToolCall, ToolDefinition

if TYPE_CHECKING:
    from agentos_core.providers.mock import MockProvider

ATTRS = "owner|region|deadline|priority|budget"
RECORD = re.compile(rf"^(?P<entity>[A-Z][\w ]*?) (?P<attr>{ATTRS}): (?P<value>\S+)\s*$")
SAID = re.compile(rf"(?P<entity>[A-Z][\w]*(?: [A-Z][\w]*)*) (?P<attr>{ATTRS}) is (?:now )?(?P<value>[^\s.]+)")
SUMMARY = re.compile(r"^Summary for (?P<entity>[^:]+): (?P<body>.+)$")
PAIR = re.compile(rf"(?P<attr>{ATTRS}) (?P<value>[^\s;]+)")


def statements(text: str) -> list[tuple[str, str, str]]:
    """(entity, attribute, value) claims made by one snippet."""
    out: list[tuple[str, str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        m = SUMMARY.match(line)
        if m:
            out += [(m["entity"].strip(), p["attr"], p["value"]) for p in PAIR.finditer(m["body"])]
            continue
        m = RECORD.match(line)
        if m:
            out.append((m["entity"], m["attr"], m["value"]))
            continue
        out += [(s["entity"], s["attr"], s["value"]) for s in SAID.finditer(line)]
    return out


def _best(claims: list[tuple[str, str, str]], entity: str, attribute: str) -> str | None:
    best: tuple[int, str] | None = None
    for ent, attr, value in claims:
        if attr == attribute and ent.lower() in entity.lower() and (best is None or len(ent) > best[0]):
            best = (len(ent), value)
    return best[1] if best else None


def memory_qa(
    provider: MockProvider, messages: list[Message], tools: list[ToolDefinition], config: ModelConfig
) -> tuple[str, list[ToolCall]]:
    from agentos_core.providers.mock import task_payload

    inp: dict[str, Any] = task_payload(messages).get("input") or {}
    q = inp.get("question") or {}
    entity, attribute = str(q.get("entity", "")), str(q.get("attribute", ""))
    said = [c for turn in inp.get("history") or [] for c in statements(str(turn))]
    remembered = [
        c for snip in inp.get("memory_context") or [] for c in statements(str(snip.get("text", "")))
    ]
    answer = _best(said, entity, attribute) or _best(remembered, entity, attribute) or "unknown"
    return json.dumps({"answer": answer}), []
