"""Runtime configuration shared by the API and worker processes.

All values come from environment variables (prefix ``AGENTOS_``) so the same
image can run in development, CI and docker compose without code changes.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEV_JWT_SECRET = "dev-insecure-jwt-secret-change-me-0000"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENTOS_", env_file=".env", extra="ignore")

    environment: Literal["development", "test", "production"] = "development"
    database_url: str = "postgresql+asyncpg://agentos:agentos@localhost:55432/agentos"
    redis_url: str = "redis://localhost:56379/0"

    # Authentication. The default secret is only accepted outside production.
    jwt_secret: SecretStr = SecretStr(DEV_JWT_SECRET)
    jwt_ttl_seconds: int = 12 * 3600
    dev_auth_enabled: bool = True
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])

    # Provider credentials are read from the conventional, unprefixed env vars.
    anthropic_api_key: SecretStr | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    openai_api_key: SecretStr | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    anthropic_model: str = "claude-opus-5-5"
    openai_model: str | None = None  # no default: set an OpenAI model ID you have verified
    planner_provider: Literal["mock", "anthropic", "openai"] = "mock"
    # Autopilot diagnosis/optimizer calls default to a low-cost model.
    autopilot_anthropic_model: str = "claude-haiku-5-5"
    autopilot_openai_model: str | None = None
    provider_timeout_seconds: float = 120.0

    # Execution mode: paid providers are blocked in code unless every flag below is set (see
    # agentos_core.providers.execution). Having an API key present never enables them.
    execution_mode: Literal["offline", "live"] = "offline"
    allow_paid_providers: bool = False
    max_real_api_spend_usd: float = Field(default=0.0, ge=0)
    enable_network_model_requests: bool = False

    # Storage
    artifact_dir: Path = Path("./var/artifacts")
    max_artifact_bytes: int = 5 * 1024 * 1024
    max_upload_bytes: int = 10 * 1024 * 1024
    max_request_bytes: int = 1024 * 1024
    max_data_file_bytes: int = 2 * 1024 * 1024

    # Engine
    lease_ttl_seconds: float = 15.0
    heartbeat_interval_seconds: float = 3.0
    poll_interval_seconds: float = 0.5
    worker_concurrency: int = 4
    approval_ttl_seconds: int = 24 * 3600
    max_dag_nodes: int = 50
    # Nested workflows (sub_workflow nodes): maximum depth below the top-level run.
    max_nested_depth: int = Field(default=3, ge=1, le=5)
    # Scheduling: a waiting step gains one priority point per this many seconds (starvation protection).
    priority_aging_seconds: float = Field(default=30.0, gt=0)
    max_run_cost_usd: float = 25.0
    default_model_call_reservation_usd: float = 0.05
    pricing_file: Path | None = None
    # Deterministic fault injection (node.inject) for demos/tests; must be off in production.
    allow_fault_injection: bool = True
    coordinator_interval_seconds: float = 1.0
    # Batch event inserts per transaction (see docs/benchmarks.md). False = one UPDATE+INSERT per event.
    event_batching: bool = True

    # Rate limits (requests per minute per user and endpoint class)
    rate_limit_per_minute: int = 120
    critical_rate_limit_per_minute: int = 30

    # Observability
    otel_enabled: bool = False
    otel_endpoint: str | None = None
    otel_service_name: str = "agentos"
    log_level: str = "INFO"
    log_json: bool = True

    @field_validator(
        "anthropic_api_key",
        "openai_api_key",
        "openai_model",
        "autopilot_openai_model",
        "otel_endpoint",
        mode="before",
    )
    @classmethod
    def _empty_is_unset(cls, v: object) -> object:
        # docker compose passes unset variables as "" - that must not count as configured.
        return None if isinstance(v, str) and not v.strip() else v

    def ensure_safe(self) -> None:
        """Refuse to start production with development-only defaults."""
        if self.environment == "production":
            secret = self.jwt_secret.get_secret_value()
            if secret == DEV_JWT_SECRET or len(secret) < 32:
                raise RuntimeError(
                    "AGENTOS_JWT_SECRET must be set to a random value of at least 32 bytes in production"
                )
            if self.dev_auth_enabled:
                raise RuntimeError("AGENTOS_DEV_AUTH_ENABLED must be false in production")
            if self.allow_fault_injection:
                raise RuntimeError("AGENTOS_ALLOW_FAULT_INJECTION must be false in production")


@lru_cache
def get_settings() -> Settings:
    return Settings()
