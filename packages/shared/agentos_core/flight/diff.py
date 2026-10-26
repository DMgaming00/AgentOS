"""Structured diff of two flight recordings: steps, branches, retries, tool calls, model responses, outputs."""

from __future__ import annotations

import hashlib
import json
from typing import Any

SNIPPET = 240


def _h(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:12]


def _snip(text: str | None) -> str | None:
    return None if text is None else (text if len(text) <= SNIPPET else text[:SNIPPET] + "…")


def json_diff(
    a: Any, b: Any, path: str = "", out: list[dict[str, Any]] | None = None, limit: int = 25
) -> list[dict[str, Any]]:
    """First ``limit`` differing JSON paths (values truncated)."""
    out = [] if out is None else out
    if len(out) >= limit:
        return out
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            child = f"{path}.{k}" if path else str(k)
            if k not in a:
                out.append(
                    {"path": child, "a": None, "b": _snip(json.dumps(b[k], default=str)), "kind": "added"}
                )
            elif k not in b:
                out.append(
                    {"path": child, "a": _snip(json.dumps(a[k], default=str)), "b": None, "kind": "removed"}
                )
            else:
                json_diff(a[k], b[k], child, out, limit)
    elif isinstance(a, list) and isinstance(b, list):
        for i in range(max(len(a), len(b))):
            if i >= len(a):
                out.append(
                    {
                        "path": f"{path}[{i}]",
                        "a": None,
                        "b": _snip(json.dumps(b[i], default=str)),
                        "kind": "added",
                    }
                )
            elif i >= len(b):
                out.append(
                    {
                        "path": f"{path}[{i}]",
                        "a": _snip(json.dumps(a[i], default=str)),
                        "b": None,
                        "kind": "removed",
                    }
                )
            else:
                json_diff(a[i], b[i], f"{path}[{i}]", out, limit)
    elif a != b:
        out.append(
            {
                "path": path or "$",
                "a": _snip(json.dumps(a, default=str)),
                "b": _snip(json.dumps(b, default=str)),
                "kind": "changed",
            }
        )
    return out[:limit]


def _calls_by_step(rec: dict[str, Any], bodies: dict[str, dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for c in rec["model_calls"]:
        if c["status"] != "succeeded":
            continue
        body = bodies.get(c["id"], {})
        resp = body.get("response") or {}
        out.setdefault(c["step_key"], []).append(
            {
                "digest": c["request_digest"],
                "text": resp.get("text"),
                "tools": [(t["name"], _h(t.get("arguments"))) for t in resp.get("tool_calls", [])],
                "latency_ms": c["latency_ms"],
                "tokens": (c.get("input_tokens") or 0) + (c.get("output_tokens") or 0),
                "model": c["model"],
            }
        )
    return out


def _tools_by_step(rec: dict[str, Any]) -> dict[str, list[tuple[str, str, str]]]:
    out: dict[str, list[tuple[str, str, str]]] = {}
    for t in rec["tool_executions"]:
        out.setdefault(t["step_key"], []).append((t["tool"], t["arguments_hash"][:12], t["status"]))
    return out


def diff_recordings(
    a: dict[str, Any],
    b: dict[str, Any],
    bodies_a: dict[str, dict[str, Any]] | None = None,
    bodies_b: dict[str, dict[str, Any]] | None = None,
    evaluation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sa = {s["key"]: s for s in a["steps"]}
    sb = {s["key"]: s for s in b["steps"]}
    ca, cb = _calls_by_step(a, bodies_a or {}), _calls_by_step(b, bodies_b or {})
    ta, tb = _tools_by_step(a), _tools_by_step(b)
    steps: list[dict[str, Any]] = []
    for key in sorted(set(sa) | set(sb), key=lambda k: (sa.get(k) or sb[k])["started_ms"] or 0):
        x, y = sa.get(key), sb.get(key)
        entry: dict[str, Any] = {
            "key": key,
            "presence": "both" if x and y else "only_a" if x else "only_b",
            "differences": [],
        }
        if x and y:
            entry["status"] = [x["status"], y["status"]]
            entry["attempts"] = [x["attempts"], y["attempts"]]
            entry["duration_ms"] = [x["duration_ms"], y["duration_ms"]]
            if x["status"] != y["status"]:
                kind = "branch" if "skipped" in (x["status"], y["status"]) else "status"
                entry["differences"].append({"kind": kind, "a": x["status"], "b": y["status"]})
            # Retries are failed attempts. Extra attempts alone are not (an approval resume starts a new attempt).
            if x["failures"] != y["failures"]:
                entry["differences"].append({"kind": "retries", "a": x["failures"], "b": y["failures"]})
            if x["output"] != y["output"]:
                entry["differences"].append({"kind": "output", "paths": json_diff(x["output"], y["output"])})
            la, lb = ca.get(key, []), cb.get(key, [])
            calls = []
            for i in range(max(len(la), len(lb))):
                p, q = (la[i] if i < len(la) else None), (lb[i] if i < len(lb) else None)
                calls.append(
                    {
                        "index": i,
                        "request_same": bool(p and q and p["digest"] == q["digest"]),
                        "response_same": bool(
                            p and q and p["text"] == q["text"] and p["tools"] == q["tools"]
                        ),
                        "a": p
                        and {
                            "text": _snip(p["text"]),
                            "tools": [t[0] for t in p["tools"]],
                            "latency_ms": p["latency_ms"],
                        },
                        "b": q
                        and {
                            "text": _snip(q["text"]),
                            "tools": [t[0] for t in q["tools"]],
                            "latency_ms": q["latency_ms"],
                        },
                    }
                )
            entry["model_calls"] = calls
            if any(not c["response_same"] for c in calls):
                entry["differences"].append(
                    {"kind": "model_response", "calls": [c["index"] for c in calls if not c["response_same"]]}
                )
            xa, xb = ta.get(key, []), tb.get(key, [])
            entry["tool_calls"] = {"a": [f"{t[0]}:{t[2]}" for t in xa], "b": [f"{t[0]}:{t[2]}" for t in xb]}
            if xa != xb:  # (tool, arguments hash, status): a refused call differs from an executed one
                first = next(
                    (i for i in range(min(len(xa), len(xb))) if xa[i] != xb[i]), min(len(xa), len(xb))
                )
                entry["differences"].append(
                    {"kind": "tool_calls", "first_divergence": first, **entry["tool_calls"]}
                )
        else:
            entry["differences"].append({"kind": "presence", "only_in": "a" if x else "b"})
        steps.append(entry)

    ra, rb = a["run"], b["run"]
    changed = [s for s in steps if s["differences"]]
    summary = {
        "steps_total": len(steps),
        "steps_different": len(changed),
        "branch_differences": [
            s["key"] for s in steps if any(d["kind"] == "branch" for d in s["differences"])
        ],
        "retry_differences": [
            s["key"] for s in steps if any(d["kind"] == "retries" for d in s["differences"])
        ],
        "final_status": [ra["status"], rb["status"]],
        "final_output_same": ra["output"] == rb["output"],
        "final_output_diff": json_diff(ra["output"], rb["output"]),
        "duration_ms": [ra["duration_ms"], rb["duration_ms"]],
        "cost_usd_estimated_simulated": [ra["cost_spent_usd"], rb["cost_spent_usd"]],
        "model_calls": [ra["model_calls"], rb["model_calls"]],
        "tool_calls": [ra["tool_calls"], rb["tool_calls"]],
        "identical": not changed and ra["status"] == rb["status"] and ra["output"] == rb["output"],
    }
    if evaluation:
        summary["evaluation"] = evaluation
    return {"a": ra["id"], "b": rb["id"], "summary": summary, "steps": steps}
