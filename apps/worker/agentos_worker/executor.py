"""Executes one claimed step and records the outcome through fenced engine writes."""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any

from opentelemetry import trace
from sqlalchemy import select

from agentos_core.autopilot.versions import AgentSpec, load_agent_spec
from agentos_core.config import get_settings
from agentos_core.db.models import AgentDefinition, Document
from agentos_core.db.session import get_sessionmaker
from agentos_core.domain.dag import AgentInfo, Limits, NodeType, resolve_refs
from agentos_core.domain.errors import (
    AgentOSError,
    ApprovalRejectedError,
    InvalidInputError,
    InvalidModelOutputError,
    LeaseLostError,
    ProviderUnavailableError,
    StepTimeoutError,
    ToolExecutionError,
    classify,
)
from agentos_core.engine.core import (
    PLANNER_NODE_KEY,
    AgentRef,
    ApprovalSpec,
    ClaimedStep,
    StaleLeaseError,
    complete_planner_step,
    complete_step,
    fail_step,
    suspend_for_approval,
)
from agentos_core.log import get_logger
from agentos_core.planner import plan_workflow
from agentos_core.providers.base import Message, ModelConfig, ModelResponse
from agentos_core.redaction import redact
from agentos_core.storage import Storage
from agentos_core.tools.registry import ToolRegistry
from agentos_core.tools.runner import StepScope, ToolCallRequest, arguments_hash, execute_tool_call
from agentos_core.util import sha256_hex
from agentos_worker.agent_runtime import AgentRuntime, Completed, Suspended
from agentos_worker.model_calls import call_model

log = get_logger(__name__)
tracer = trace.get_tracer("agentos.worker")


class _Done:
    """Marker: the dispatcher already persisted the outcome."""


class StepExecutor:
    def __init__(self, registry: ToolRegistry, storage: Storage, worker_id: str) -> None:
        self.registry = registry
        self.storage = storage
        self.worker_id = worker_id
        self.runtime = AgentRuntime(registry)

    async def execute(self, claim: ClaimedStep) -> str:
        """Run a step to an outcome. Returns a short status string for logging/metrics."""
        cfg = claim.node_config
        timeout = float(cfg.get("timeout_seconds", 300))
        with tracer.start_as_current_span(
            "step.execute",
            attributes={
                "agentos.run_id": str(claim.run_id),
                "agentos.step_id": str(claim.step_id),
                "agentos.node_key": claim.node_key,
                "agentos.node_type": claim.node_type,
                "agentos.attempt": claim.attempt_number,
                "agentos.worker_id": self.worker_id,
            },
        ) as span:
            try:
                async with asyncio.timeout(timeout):
                    await self._inject(claim)
                    result = await self._dispatch(claim)
            except LeaseLostError:
                log.warning("lease_lost_during_execution", node_key=claim.node_key, run_id=str(claim.run_id))
                return "lease_lost"
            except TimeoutError:
                return await self._fail(
                    claim, StepTimeoutError(f"step exceeded timeout_seconds={timeout:g}"), span
                )
            except AgentOSError as exc:
                return await self._fail(claim, exc, span)
            except StaleLeaseError:
                return "stale"
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("step_unexpected_error", node_key=claim.node_key)
                return await self._fail(claim, classify(exc), span)

            try:
                if isinstance(result, Completed):
                    await complete_step(
                        claim.step_id, claim.lease_token, result.output, metadata=result.metadata
                    )
                    return "succeeded"
                if isinstance(result, Suspended):
                    await suspend_for_approval(
                        claim.step_id,
                        claim.lease_token,
                        checkpoint=result.checkpoint,
                        approval=result.approval,
                    )
                    return "suspended"
                return "succeeded"
            except StaleLeaseError:
                log.warning("stale_result_rejected", node_key=claim.node_key, attempt=claim.attempt_number)
                return "stale"

    async def _fail(self, claim: ClaimedStep, err: AgentOSError, span: trace.Span) -> str:
        span.set_status(trace.Status(trace.StatusCode.ERROR, err.error_type))
        try:
            status = await fail_step(claim.step_id, claim.lease_token, err, run_id=claim.run_id)
        except StaleLeaseError:
            return "stale"
        log.info("step_failed", node_key=claim.node_key, error=err.error_type, outcome=str(status))
        return f"failed:{status}"

    # ------------------------------------------------------------------ fault injection

    async def _inject(self, claim: ClaimedStep) -> None:
        inject = claim.node_config.get("inject") or {}
        if not inject:
            return
        if not get_settings().allow_fault_injection:
            raise InvalidInputError("fault injection is disabled in this environment")
        if delay := float(inject.get("sleep_seconds", 0)):
            await asyncio.sleep(delay)
        crash_on = _as_list(inject.get("crash_worker_on_attempt"))
        if claim.attempt_number in crash_on:
            await asyncio.sleep(float(inject.get("crash_after_seconds", 0)))
            log.warning(
                "fault_injection_crashing_worker", node_key=claim.node_key, attempt=claim.attempt_number
            )
            os._exit(137)  # simulate SIGKILL: no cleanup, no lease release
        if claim.attempt_number in _as_list(inject.get("fail_on_attempts")):
            if inject.get("fail_error") == "permanent":
                raise ToolExecutionError(f"injected permanent failure on attempt {claim.attempt_number}")
            raise ProviderUnavailableError(f"injected transient failure on attempt {claim.attempt_number}")

    # ------------------------------------------------------------------ dispatch

    async def _dispatch(self, claim: ClaimedStep) -> Completed | Suspended | _Done:
        context = {
            "input": claim.run_input,
            "steps": {k: {"output": v} for k, v in claim.dependency_outputs.items()},
        }
        try:
            resolved_input = resolve_refs(claim.step_input, context)
        except KeyError as exc:
            raise InvalidInputError(f"unresolved reference {exc.args[0]!r}") from exc

        if claim.node_type == "plan":
            return await self._plan(claim)
        if claim.node_type == NodeType.AGENT_TASK:
            return await self._agent(claim, resolved_input)
        if claim.node_type == NodeType.TOOL_TASK:
            return await self._tool(claim, context)
        if claim.node_type == NodeType.APPROVAL_GATE:
            return self._approval_gate(claim, resolved_input)
        if claim.node_type == NodeType.AGGREGATION:
            return self._aggregate(claim, resolved_input)
        raise InvalidInputError(f"unsupported node type {claim.node_type}")

    async def _load_agent(self, agent_id: uuid.UUID | None, version_id: uuid.UUID | None) -> AgentSpec:
        """The pinned, immutable agent version for this step (or the agent row for pre-0002 runs)."""
        if agent_id is None:
            raise InvalidInputError("agent_task without agent")
        async with get_sessionmaker()() as session:
            return await load_agent_spec(session, agent_id, version_id)

    def _scope(self, claim: ClaimedStep, allowed: set[str], agent_name: str | None) -> StepScope:
        return StepScope(
            workspace_id=claim.workspace_id,
            run_id=claim.run_id,
            step_id=claim.step_id,
            node_key=claim.node_key,
            attempt_id=claim.attempt_id,
            lease_token=claim.lease_token,
            agent_name=agent_name,
            allowed_tools=frozenset(allowed),
            storage=self.storage,
        )

    async def _agent(self, claim: ClaimedStep, resolved_input: dict[str, Any]) -> Completed | Suspended:
        agent = await self._load_agent(claim.agent_definition_id, claim.agent_version_id)
        agent_tools = set(agent.allowed_tool_ids or [])
        node_tools = claim.node_config.get("tools")
        # Effective permission = agent allow-list ∩ node policy ∩ registered tools.
        allowed = (agent_tools if node_tools is None else agent_tools & set(node_tools)) & set(
            self.registry.names()
        )
        scope = self._scope(claim, allowed, agent.name)
        return await self.runtime.run(
            claim, agent, scope, resolved_input, after_tool=self._after_tool_hook(claim)
        )

    def _after_tool_hook(self, claim: ClaimedStep) -> Any:
        crash_tool = (claim.node_config.get("inject") or {}).get("crash_after_tool")
        if not crash_tool:
            return None

        async def hook(name: str, outcome: Any) -> None:
            first_execution = (
                outcome.kind == "result"
                and not outcome.content.get("idempotent_replay")
                and not outcome.content.get("deduplicated")
            )
            if name == crash_tool and first_execution:
                # Crash after the side effect but before the step records it: the retry must not duplicate it.
                log.warning("fault_injection_crash_after_tool", tool=name, node_key=claim.node_key)
                os._exit(137)

        return hook

    async def _tool(self, claim: ClaimedStep, context: dict[str, Any]) -> Completed | Suspended:
        cfg = claim.node_config
        tool_name = cfg["tool"]
        try:
            arguments = resolve_refs(cfg.get("arguments") or {}, context)
        except KeyError as exc:
            raise InvalidInputError(f"unresolved reference {exc.args[0]!r}") from exc
        scope = self._scope(claim, {tool_name}, None)
        # A stable call id lets a resumed attempt find the approval for this exact call.
        call = ToolCallRequest(call_id=f"node:{claim.node_key}", name=tool_name, arguments=arguments)
        outcome = await execute_tool_call(call, scope, self.registry)
        if outcome.kind == "needs_approval":
            assert outcome.approval is not None
            return Suspended(approval=outcome.approval, checkpoint={"tool_task": True})
        if outcome.kind == "result":
            return Completed(output=outcome.content, metadata={"tool": tool_name})
        if outcome.extra.get("rejected"):
            raise ApprovalRejectedError(f"{tool_name} was not approved: {outcome.content.get('decision')}")
        raise ToolExecutionError(
            str(outcome.content.get("error", "tool failed")), details={"tool": tool_name}
        )

    def _approval_gate(self, claim: ClaimedStep, resolved_input: dict[str, Any]) -> Suspended:
        cfg = claim.node_config
        args = {"action": cfg.get("action"), "input": resolved_input}
        return Suspended(
            approval=ApprovalSpec(
                requested_action=str(cfg.get("action")),
                reason=cfg.get("reason"),
                affected_resource=resolved_input.get("resource")
                if isinstance(resolved_input.get("resource"), str)
                else None,
                requesting_agent=None,
                sanitized_arguments=redact(args),
                arguments_hash=arguments_hash("approval_gate", args),
            ),
            checkpoint={"approval_gate": True},
        )

    def _aggregate(self, claim: ClaimedStep, resolved_input: dict[str, Any]) -> Completed:
        mode = claim.node_config.get("mode") or "collect"
        deps = {k: claim.dependency_outputs.get(k) for k in sorted(claim.dependency_outputs)}
        if mode == "merge":
            merged: dict[str, Any] = {}
            for value in deps.values():
                if isinstance(value, dict):
                    merged.update(value)
            out: dict[str, Any] = {"merged": merged}
        else:
            out = {"inputs": deps}
        out["input"] = resolved_input
        out["fingerprint"] = sha256_hex(repr(sorted(deps)))[:16]
        return Completed(output=out, metadata={"mode": mode})

    async def _plan(self, claim: ClaimedStep) -> _Done:
        settings = get_settings()
        async with get_sessionmaker()() as session:
            agents = list(
                (
                    await session.execute(
                        select(AgentDefinition).where(AgentDefinition.workspace_id == claim.workspace_id)
                    )
                ).scalars()
            )
            docs = (
                await session.execute(
                    select(Document.filename, Document.title, Document.content_type)
                    .where(Document.workspace_id == claim.workspace_id)
                    .order_by(Document.filename)
                )
            ).all()
        catalog = {
            "agents": [
                {"name": a.name, "description": a.description, "allowed_tools": a.allowed_tool_ids}
                for a in agents
            ],
            "tools": [{"name": t["name"], "description": t["description"]} for t in self.registry.catalog()],
            "documents": [{"filename": f, "title": t, "content_type": c} for f, t, c in docs],
        }
        provider = str(claim.run_input.get("planner_provider") or settings.planner_provider)
        if provider == "mock":
            model = "mock-planner"
            extra = {"scenario": claim.run_input.get("planner_scenario", "planner")}
        else:
            model = settings.anthropic_model if provider == "anthropic" else (settings.openai_model or "")
            extra = {}
        config = ModelConfig(
            model=model,
            max_tokens=8000,
            extra=extra,
            metadata={"attempt_number": claim.attempt_number, "node_key": PLANNER_NODE_KEY},
        )

        async def _call(system: str, msgs: list[Message], cfg: ModelConfig) -> ModelResponse:
            return await call_model(
                provider_name=provider,
                system=system,
                messages=msgs,
                tools=[],
                config=cfg,
                run_id=claim.run_id,
                step_id=claim.step_id,
                attempt_id=claim.attempt_id,
                agent_definition_id=None,
                node_key=PLANNER_NODE_KEY,
            )

        outcome = await plan_workflow(
            task=str(claim.step_input.get("task", "")),
            catalog=catalog,
            agents={a.name: AgentInfo(a.name, frozenset(a.allowed_tool_ids or [])) for a in agents},
            tools=self.registry.infos(),
            limits=Limits(max_nodes=settings.max_dag_nodes, max_cost_usd=settings.max_run_cost_usd),
            config=config,
            call_model=_call,
        )
        meta = {
            "planner_provider": provider,
            "planner_attempts": len(outcome.attempts),
            "attempt_issues": [a.issues for a in outcome.attempts],
        }
        if not outcome.ok or outcome.result is None or outcome.result.spec is None:
            raise InvalidModelOutputError(
                f"planner produced no valid plan after {len(outcome.attempts)} attempts",
                details={"issues": meta["attempt_issues"]},
            )
        await complete_planner_step(
            claim.step_id,
            claim.lease_token,
            spec=outcome.result.spec,
            agents_by_name={a.name: AgentRef(a.id, a.name) for a in agents},
            plan_meta=meta,
        )
        return _Done()


def _as_list(value: Any) -> list[int]:
    if value is None:
        return []
    if isinstance(value, list):
        return [int(v) for v in value]
    return [int(value)]
