"""Create arena runs, schedule evaluations, enforce the test-split lock, report progress.

Anti-leakage rules enforced here, in code:

* creating an arena run schedules only the *development* and *validation* datasets;
* the *test* datasets are scheduled by ``start_final_test``, which requires every selection evaluation to be complete,
  can happen once per arena run (row lock + ``final_test_started_at``), and creates entries under a unique
  (arena, version, dataset) key, so a contender is never scored on test twice in one arena run;
* contenders must be offline (mock/fixture/recorded) providers, unless an administrator explicitly creates the run with
  ``allow_live`` while paid requests are enabled (execution policy + spend cap); such a run records the live contenders
  in its configuration and is no longer labeled offline-only.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from agentos_core.arena.corpus import FAMILIES, SELECTION_SPLITS, import_corpus, load_manifest
from agentos_core.autopilot.config import AgentConfig
from agentos_core.autopilot.evaluation import EvaluationError, create_evaluation_run
from agentos_core.autopilot.policy import load_policy
from agentos_core.db.models import (
    AgentConfigVersion,
    AgentDefinition,
    ArenaEntry,
    ArenaRun,
    EvaluationDataset,
    EvaluationRun,
    Workspace,
)
from agentos_core.util import canonical_json, sha256_hex, utcnow

OFFLINE_PROVIDERS = frozenset({"mock", "fixture", "recorded"})
MAX_CONTENDERS = 12


class ArenaError(EvaluationError):
    pass


async def _schedule(
    session: AsyncSession,
    arena: ArenaRun,
    contenders: list[tuple[AgentDefinition, AgentConfigVersion]],
    datasets: dict[tuple[str, str], EvaluationDataset],
    splits: tuple[str, ...],
    *,
    created_by: uuid.UUID | None,
) -> int:
    ws = await session.get(Workspace, arena.workspace_id)
    policy = load_policy(ws.settings if ws else {})
    count = 0
    for agent, version in contenders:
        for family in FAMILIES:
            for split in splits:
                dataset = datasets[(family, split)]
                ev = await create_evaluation_run(
                    session,
                    agent=agent,
                    version=version,
                    dataset=dataset,
                    trials=1,
                    created_by=created_by,
                    max_concurrency=policy.max_eval_concurrency,
                    per_case_max_cost_usd=policy.per_case_max_cost_usd,
                )
                session.add(
                    ArenaEntry(
                        id=uuid.uuid4(),
                        arena_run_id=arena.id,
                        agent_id=agent.id,
                        agent_version_id=version.id,
                        dataset_id=dataset.id,
                        family=family,
                        split=split,
                        evaluation_run_id=ev.id,
                    )
                )
                count += 1
    await session.flush()
    return count


async def _load_contenders(
    session: AsyncSession, workspace_id: uuid.UUID, version_ids: list[uuid.UUID], *, allow_live: bool = False
) -> list[tuple[AgentDefinition, AgentConfigVersion]]:
    if not 2 <= len(set(version_ids)) <= MAX_CONTENDERS or len(set(version_ids)) != len(version_ids):
        raise ArenaError(
            f"choose between 2 and {MAX_CONTENDERS} distinct agent versions",
            code="invalid_contenders",
            status=422,
        )
    out = []
    for vid in version_ids:
        version = await session.get(AgentConfigVersion, vid)
        agent = await session.get(AgentDefinition, version.agent_id) if version else None
        if version is None or agent is None or agent.workspace_id != workspace_id:
            raise ArenaError("agent version not found", code="not_found", status=404)
        provider = AgentConfig.model_validate(version.config_snapshot).provider
        if provider not in OFFLINE_PROVIDERS and allow_live:
            from agentos_core.providers.execution import ExecutionPolicy

            policy = ExecutionPolicy.current()
            if not policy.paid_requests_allowed:
                raise ArenaError(
                    "live contenders need paid requests enabled: " + "; ".join(policy.blockers()),
                    code="paid_providers_blocked",
                    status=409,
                )
        elif provider not in OFFLINE_PROVIDERS:
            raise ArenaError(
                f"{agent.name} uses provider {provider!r}; the Arena runs offline providers only "
                f"({', '.join(sorted(OFFLINE_PROVIDERS))})",
                code="offline_only",
                status=422,
            )
        out.append((agent, version))
    if len({a.id for a, _ in out}) != len(out):
        raise ArenaError("each agent may appear once per arena run", code="invalid_contenders", status=422)
    return out


async def create_arena(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID | None,
    name: str,
    agent_version_ids: list[uuid.UUID],
    allow_live: bool = False,
) -> ArenaRun:
    contenders = await _load_contenders(session, workspace_id, agent_version_ids, allow_live=allow_live)
    live = [
        a.name
        for a, v in contenders
        if AgentConfig.model_validate(v.config_snapshot).provider not in OFFLINE_PROVIDERS
    ]
    datasets = await import_corpus(session, workspace_id, created_by=user_id)
    manifest = load_manifest()
    arena = ArenaRun(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        name=name,
        created_by=user_id,
        config={
            "corpus": manifest["corpus"],
            "corpus_version": manifest["version"],
            "split_salt": manifest["split_salt"],
            "manifest_hash": sha256_hex(canonical_json(manifest["datasets"])),
            "dataset_hashes": {f"{f}/{s}": d.dataset_hash for (f, s), d in sorted(datasets.items())},
            "contenders": [
                {
                    "agent": a.name,
                    "agent_id": str(a.id),
                    "version": v.version,
                    "version_id": str(v.id),
                    "config_hash": v.config_hash,
                }
                for a, v in contenders
            ],
            "trials": 1,
            "scoring": "unweighted mean of family pass rates; ties by paired bootstrap",
            "offline_only": not live,
            "live_contenders": live,
        },
    )
    session.add(arena)
    await session.flush()
    await _schedule(session, arena, contenders, datasets, SELECTION_SPLITS, created_by=user_id)
    return arena


async def _status_counts(session: AsyncSession, arena_id: uuid.UUID) -> dict[str, dict[str, int]]:
    rows = (
        await session.execute(
            select(ArenaEntry.split, EvaluationRun.status, func.count())
            .join(EvaluationRun, EvaluationRun.id == ArenaEntry.evaluation_run_id)
            .where(ArenaEntry.arena_run_id == arena_id)
            .group_by(ArenaEntry.split, EvaluationRun.status)
        )
    ).all()
    out: dict[str, dict[str, int]] = {}
    for split, status, n in rows:
        out.setdefault(split, {})[status] = n
    return out


def derive_stage(arena: ArenaRun, counts: dict[str, dict[str, int]]) -> str:
    """selection_running | selection_complete | final_test_running | completed | failed."""
    if any(s in ("failed", "cancelled") for c in counts.values() for s in c):
        return "failed"

    def done(splits: tuple[str, ...]) -> bool:
        return all(set(counts.get(s, {})) <= {"completed"} for s in splits)

    if arena.final_test_started_at is None:
        return "selection_complete" if done(SELECTION_SPLITS) else "selection_running"
    return "completed" if done(("development", "validation", "test")) else "final_test_running"


async def arena_summary(session: AsyncSession, arena: ArenaRun) -> dict[str, Any]:
    counts = await _status_counts(session, arena.id)
    return {
        "id": str(arena.id),
        "name": arena.name,
        "created_at": arena.created_at.isoformat(),
        "stage": derive_stage(arena, counts),
        "final_test_started_at": arena.final_test_started_at.isoformat()
        if arena.final_test_started_at
        else None,
        "progress": counts,
        "config": arena.config,
    }


async def start_final_test(
    session: AsyncSession, arena_id: uuid.UUID, *, user_id: uuid.UUID | None
) -> ArenaRun:
    arena = (
        await session.execute(select(ArenaRun).where(ArenaRun.id == arena_id).with_for_update())
    ).scalar_one_or_none()
    if arena is None:
        raise ArenaError("arena run not found", code="not_found", status=404)
    if arena.final_test_started_at is not None:
        raise ArenaError("the test split has already been scored for this arena run", code="test_already_run")
    counts = await _status_counts(session, arena.id)
    stage = derive_stage(arena, counts)
    if stage != "selection_complete":
        raise ArenaError(
            f"the test split is unlocked only after development and validation finish (stage: {stage})",
            code="selection_not_complete",
        )
    contenders = []
    for c in arena.config["contenders"]:
        version = await session.get(AgentConfigVersion, uuid.UUID(c["version_id"]))
        agent = await session.get(AgentDefinition, uuid.UUID(c["agent_id"]))
        assert version is not None and agent is not None
        if (
            version.config_hash != c["config_hash"]
        ):  # configurations are immutable; this would be a corrupted record
            raise ArenaError("a contender's configuration changed since selection", code="config_changed")
        contenders.append((agent, version))
    datasets = await import_corpus(session, arena.workspace_id, created_by=user_id)
    for (family, split), d in datasets.items():
        recorded = arena.config["dataset_hashes"].get(f"{family}/{split}")
        if recorded != d.dataset_hash:
            raise ArenaError(
                f"dataset {family}/{split} changed since the arena run started", code="corpus_changed"
            )
    arena.final_test_started_at = utcnow()
    arena.final_test_by = user_id
    await _schedule(session, arena, contenders, datasets, ("test",), created_by=user_id)
    return arena
