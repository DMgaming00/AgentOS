"""release center: environments, release plans (canary schedule, rollback rules), release events

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-26 21:31:05.756938
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_environment_pins",
        sa.Column("agent_id", sa.UUID(), nullable=False),
        sa.Column("environment", sa.String(length=16), nullable=False),
        sa.Column("version_id", sa.UUID(), nullable=False),
        sa.Column("updated_by", sa.UUID(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "environment in ('development','staging')",
            name=op.f("ck_agent_environment_pins_environment_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agent_definitions.id"],
            name=op.f("fk_agent_environment_pins_agent_id_agent_definitions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"],
            ["users.id"],
            name=op.f("fk_agent_environment_pins_updated_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["agent_config_versions.id"],
            name=op.f("fk_agent_environment_pins_version_id_agent_config_versions"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("agent_id", "environment", name=op.f("pk_agent_environment_pins")),
    )
    op.create_table(
        "release_plans",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("agent_id", sa.UUID(), nullable=False),
        sa.Column("environment", sa.String(length=16), nullable=False),
        sa.Column("candidate_version_id", sa.UUID(), nullable=False),
        sa.Column("baseline_version_id", sa.UUID(), nullable=False),
        sa.Column("experiment_id", sa.UUID(), nullable=True),
        sa.Column("request_id", sa.UUID(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("schedule", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("stage_index", sa.Integer(), nullable=False),
        sa.Column("seed", sa.String(length=64), nullable=False),
        sa.Column(
            "rules",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("auto_rollback", sa.Boolean(), nullable=False),
        sa.Column("min_stage_samples", sa.Integer(), nullable=False),
        sa.Column("min_stage_seconds", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("stage_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_reason", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "environment in ('development','staging','production')",
            name=op.f("ck_release_plans_environment_valid"),
        ),
        sa.CheckConstraint(
            "status in ('pending_approval','active','completed','rolled_back','aborted')",
            name=op.f("ck_release_plans_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agent_definitions.id"],
            name=op.f("fk_release_plans_agent_id_agent_definitions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["baseline_version_id"],
            ["agent_config_versions.id"],
            name=op.f("fk_release_plans_baseline_version_id_agent_config_versions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_version_id"],
            ["agent_config_versions.id"],
            name=op.f("fk_release_plans_candidate_version_id_agent_config_versions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_release_plans_created_by_users"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["optimization_experiments.id"],
            name=op.f("fk_release_plans_experiment_id_optimization_experiments"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["request_id"],
            ["deployment_requests.id"],
            name=op.f("fk_release_plans_request_id_deployment_requests"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_release_plans_workspace_id_workspaces"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_release_plans")),
    )
    op.create_index("ix_release_plans_agent", "release_plans", ["agent_id", "status"], unique=False)
    op.create_index(
        "ix_release_plans_ws_created", "release_plans", ["workspace_id", "created_at"], unique=False
    )
    op.create_index(
        "uq_release_plans_live",
        "release_plans",
        ["agent_id", "environment"],
        unique=True,
        postgresql_where=sa.text("status in ('pending_approval','active')"),
    )
    op.create_table(
        "release_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("plan_id", sa.UUID(), nullable=False),
        sa.Column("event_type", sa.String(length=48), nullable=False),
        sa.Column("actor", sa.String(length=100), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["release_plans.id"],
            name=op.f("fk_release_events_plan_id_release_plans"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_release_events")),
    )
    op.create_index("ix_release_events_plan", "release_events", ["plan_id", "id"], unique=False)
    op.add_column("agent_definitions", sa.Column("canary_seed", sa.String(length=64), nullable=True))
    op.add_column(
        "workflow_runs",
        sa.Column("environment", sa.String(length=16), server_default="production", nullable=False),
    )

    op.create_check_constraint(
        op.f("ck_workflow_runs_environment_valid"),
        "workflow_runs",
        "environment in ('development','staging','production')",
    )
    op.execute(
        "CREATE TRIGGER release_events_immutable BEFORE UPDATE OR DELETE ON release_events "
        "FOR EACH ROW EXECUTE FUNCTION agentos_forbid_change()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS release_events_immutable ON release_events")
    op.drop_constraint(op.f("ck_workflow_runs_environment_valid"), "workflow_runs", type_="check")
    op.drop_column("workflow_runs", "environment")
    op.drop_column("agent_definitions", "canary_seed")
    op.drop_index("ix_release_events_plan", table_name="release_events")
    op.drop_table("release_events")
    op.drop_index(
        "uq_release_plans_live",
        table_name="release_plans",
        postgresql_where=sa.text("status in ('pending_approval','active')"),
    )
    op.drop_index("ix_release_plans_ws_created", table_name="release_plans")
    op.drop_index("ix_release_plans_agent", table_name="release_plans")
    op.drop_table("release_plans")
    op.drop_table("agent_environment_pins")
