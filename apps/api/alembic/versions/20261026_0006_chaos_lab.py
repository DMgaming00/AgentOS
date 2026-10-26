"""chaos lab: fault scenarios, experiments, injections, chaos runs

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-26 17:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PURPOSE = "ck_workflow_runs_purpose_valid"
JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "fault_scenarios",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("builtin", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("plan", JSONB, nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_fault_scenarios_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_fault_scenarios_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fault_scenarios")),
        sa.UniqueConstraint("workspace_id", "name", name="uq_fault_scenarios_ws_name"),
    )
    op.create_table(
        "chaos_experiments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("workflow_definition_id", sa.Uuid(), nullable=False),
        sa.Column("plan", JSONB, nullable=False),
        sa.Column("plan_sha256", sa.String(length=64), nullable=False),
        sa.Column("version_overrides", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("conditions", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("results", JSONB, nullable=True),
        sa.Column("abort_reason", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status in ('running','completed','failed','cancelled')",
            name=op.f("ck_chaos_experiments_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_chaos_experiments_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workflow_definition_id"],
            ["workflow_definitions.id"],
            name=op.f("fk_chaos_experiments_workflow_definition_id_workflow_definitions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_chaos_experiments_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chaos_experiments")),
    )
    op.create_index("ix_chaos_experiments_ws", "chaos_experiments", ["workspace_id", "created_at"])
    op.add_column("workflow_runs", sa.Column("chaos_experiment_id", sa.Uuid(), nullable=True))
    op.add_column("workflow_runs", sa.Column("chaos_case_index", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_workflow_runs_chaos_experiment",
        "workflow_runs",
        "chaos_experiments",
        ["chaos_experiment_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "uq_workflow_runs_chaos_case",
        "workflow_runs",
        ["chaos_experiment_id", "chaos_case_index"],
        unique=True,
        postgresql_where=sa.text("chaos_experiment_id is not null"),
    )
    op.drop_constraint(op.f(PURPOSE), "workflow_runs", type_="check")
    op.create_check_constraint(
        op.f(PURPOSE), "workflow_runs", "purpose in ('workflow','evaluation','replay','chaos')"
    )
    op.create_table(
        "chaos_injections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("experiment_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("case_index", sa.Integer(), nullable=False),
        sa.Column("fault_index", sa.Integer(), nullable=False),
        sa.Column("fault_type", sa.String(length=48), nullable=False),
        sa.Column("point", sa.String(length=24), nullable=False),
        sa.Column("step_key", sa.String(length=100), nullable=True),
        sa.Column("attempt_number", sa.Integer(), nullable=True),
        sa.Column("call_key", sa.String(length=200), server_default="", nullable=False),
        sa.Column("detail", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("outcome", JSONB, nullable=True),
        sa.Column("injected_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["chaos_experiments.id"],
            name=op.f("fk_chaos_injections_experiment_id_chaos_experiments"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["workflow_runs.id"],
            name=op.f("fk_chaos_injections_run_id_workflow_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chaos_injections")),
    )
    op.create_index("ix_chaos_injections_experiment", "chaos_injections", ["experiment_id", "injected_at"])
    op.create_index("ix_chaos_injections_run", "chaos_injections", ["run_id"])
    # Injections are evidence: append-only, except that the response ``outcome`` may be filled in exactly once.
    op.execute("""
CREATE OR REPLACE FUNCTION agentos_chaos_outcome_once() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'agentos: DELETE on chaos_injections is not allowed (immutable audit record)' USING ERRCODE = 'restrict_violation';
  END IF;
  IF OLD.outcome IS NOT NULL OR (to_jsonb(OLD) - 'outcome') <> (to_jsonb(NEW) - 'outcome') THEN
    RAISE EXCEPTION 'agentos: chaos_injections are immutable; only a missing outcome may be set once' USING ERRCODE = 'restrict_violation';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
""")
    op.execute(
        "CREATE TRIGGER chaos_injections_guard BEFORE UPDATE OR DELETE ON chaos_injections "
        "FOR EACH ROW EXECUTE FUNCTION agentos_chaos_outcome_once()"
    )
    # A finished experiment (completed, failed or cancelled) is sealed: results cannot be rewritten.
    op.execute(
        "CREATE TRIGGER chaos_experiments_sealed BEFORE UPDATE OR DELETE ON chaos_experiments "
        "FOR EACH ROW EXECUTE FUNCTION agentos_seal_completed()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS chaos_experiments_sealed ON chaos_experiments")
    op.execute("DROP TRIGGER IF EXISTS chaos_injections_guard ON chaos_injections")
    op.execute("DROP FUNCTION IF EXISTS agentos_chaos_outcome_once()")
    op.drop_table("chaos_injections")
    # Fails (by design) if chaos runs exist: history is never rewritten.
    op.drop_constraint(op.f(PURPOSE), "workflow_runs", type_="check")
    op.create_check_constraint(
        op.f(PURPOSE), "workflow_runs", "purpose in ('workflow','evaluation','replay')"
    )
    op.drop_index("uq_workflow_runs_chaos_case", table_name="workflow_runs")
    op.drop_constraint("fk_workflow_runs_chaos_experiment", "workflow_runs", type_="foreignkey")
    op.drop_column("workflow_runs", "chaos_case_index")
    op.drop_column("workflow_runs", "chaos_experiment_id")
    op.drop_index("ix_chaos_experiments_ws", table_name="chaos_experiments")
    op.drop_table("chaos_experiments")
    op.drop_table("fault_scenarios")
