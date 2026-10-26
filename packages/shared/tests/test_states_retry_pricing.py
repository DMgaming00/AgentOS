import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from agentos_core.domain import errors as E
from agentos_core.domain.states import (
    IllegalTransitionError,
    RunStatus,
    StepStatus,
    assert_run_transition,
    assert_step_transition,
    can_transition_step,
)
from agentos_core.engine.retry import backoff_seconds, should_retry
from agentos_core.pricing import PricingTable, get_pricing


def test_legal_and_illegal_step_transitions():
    assert_step_transition(StepStatus.PENDING, StepStatus.READY)
    assert_step_transition(StepStatus.RUNNING, StepStatus.RETRY_WAIT)
    assert_step_transition(StepStatus.WAITING_APPROVAL, StepStatus.READY)
    for terminal in (StepStatus.SUCCEEDED, StepStatus.FAILED, StepStatus.SKIPPED, StepStatus.CANCELLED):
        for target in StepStatus:
            assert not can_transition_step(terminal, target)
    with pytest.raises(IllegalTransitionError):
        assert_step_transition(StepStatus.PENDING, StepStatus.RUNNING)  # must be ready first
    with pytest.raises(IllegalTransitionError):
        assert_step_transition(StepStatus.SUCCEEDED, StepStatus.RUNNING)


def test_run_transitions():
    assert_run_transition(RunStatus.PENDING, RunStatus.RUNNING)
    assert_run_transition(RunStatus.RUNNING, RunStatus.WAITING_APPROVAL)
    with pytest.raises(IllegalTransitionError):
        assert_run_transition(RunStatus.SUCCEEDED, RunStatus.RUNNING)
    with pytest.raises(IllegalTransitionError):
        assert_run_transition(RunStatus.CANCELLED, RunStatus.RUNNING)


def test_backoff_exponential_capped_and_jittered():
    assert [backoff_seconds(n, base=1, cap=10, jitter=False) for n in range(1, 6)] == [1, 2, 4, 8, 10]
    rng = random.Random(42)
    vals = [backoff_seconds(3, base=1, cap=10, rng=rng) for _ in range(200)]
    assert all(0 <= v <= 4 for v in vals) and len(set(vals)) > 100


@pytest.mark.parametrize(
    ("exc", "retryable"),
    [
        (E.ProviderRateLimitError("x"), True),
        (E.ProviderTimeoutError("x"), True),
        (E.ProviderUnavailableError("x"), True),
        (E.WorkerLostError("x"), True),
        (E.InvalidInputError("x"), False),
        (E.ToolNotAllowedError("x"), False),
        (E.PolicyViolationError("x"), False),
        (E.BudgetExceededError("x"), False),
        (E.ProviderAuthError("x"), False),
        (E.ApprovalRejectedError("x"), False),
    ],
)
def test_retry_classification(exc, retryable):
    now = datetime.now(UTC)
    assert (
        should_retry(exc, failure_count=1, max_attempts=3, now=now, retry_deadline_at=None, next_delay=1)
        is retryable
    )


def test_retry_respects_max_attempts_and_deadline():
    err = E.ProviderTimeoutError("x")
    now = datetime.now(UTC)
    assert not should_retry(
        err, failure_count=3, max_attempts=3, now=now, retry_deadline_at=None, next_delay=1
    )
    assert not should_retry(
        err,
        failure_count=1,
        max_attempts=3,
        now=now,
        retry_deadline_at=now + timedelta(seconds=0.5),
        next_delay=1,
    )


def test_classify_unknown_is_permanent_and_timeout_transient():
    assert not E.classify(RuntimeError("boom")).retryable
    assert E.classify(TimeoutError()).retryable


def test_pricing_actual_partial_unknown_and_simulated():
    t = PricingTable(
        {
            "version": "v1",
            "models": {
                "anthropic/m": {"input": 4, "output": 20, "cached_input": 0.2},
                "mock/*": {"input": 1, "output": 2, "simulated": True},
            },
        }
    )
    est = t.estimate(
        "anthropic", "m", input_tokens=1_000_000, output_tokens=500_000, cached_input_tokens=1_000_000
    )
    assert est.usd == Decimal("14.2") and est.basis == "actual" and est.pricing_version == "v1"
    assert t.estimate("anthropic", "m", input_tokens=1000, output_tokens=None).basis == "partial"
    assert t.estimate("anthropic", "m", input_tokens=None, output_tokens=None).usd is None
    assert t.estimate("anthropic", "unknown-model", input_tokens=10, output_tokens=10).basis == "unknown"
    assert t.estimate("mock", "anything", input_tokens=1_000_000, output_tokens=0).basis == "simulated"


def test_bundled_pricing_loads():
    table = get_pricing()
    assert table.lookup("anthropic", "claude-opus-5-5") is not None
    assert table.lookup("mock", "mock-research").simulated


def test_empty_provider_settings_are_unset(monkeypatch):
    from agentos_core.config import Settings

    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("AGENTOS_OPENAI_MODEL", " ")
    s = Settings()
    assert s.anthropic_api_key is None and s.openai_model is None


def test_database_connection_errors_are_transient_but_query_errors_are_not():
    import asyncpg
    from sqlalchemy.exc import DBAPIError, IntegrityError

    from agentos_core.domain.errors import DatabaseUnavailableError, classify

    lost = DBAPIError(
        "SELECT 1",
        {},
        asyncpg.exceptions.ConnectionDoesNotExistError("connection was closed"),
        connection_invalidated=True,
    )
    assert isinstance(classify(lost), DatabaseUnavailableError) and classify(lost).retryable
    assert classify(ConnectionResetError("reset")).retryable
    wrapped = RuntimeError("boom")
    wrapped.__cause__ = asyncpg.exceptions.AdminShutdownError(
        "terminating connection due to administrator command"
    )
    assert classify(wrapped).error_type == "database_unavailable"
    constraint = IntegrityError("INSERT", {}, asyncpg.exceptions.UniqueViolationError("duplicate key"))
    assert not classify(constraint).retryable  # a constraint violation is a bug, not an outage
    assert not classify(ValueError("x")).retryable
