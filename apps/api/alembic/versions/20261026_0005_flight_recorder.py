"""flight recorder: sanitized model call records, event span ids, replay runs

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-26 15:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PURPOSE = "ck_workflow_runs_purpose_valid"


def upgrade() -> None:
    op.create_table(
        "model_call_records",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("step_id", sa.Uuid(), nullable=True),
        sa.Column("attempt_id", sa.Uuid(), nullable=True),
        sa.Column("node_key", sa.String(length=100), nullable=False),
        sa.Column("call_index", sa.Integer(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=True),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("agent_version_id", sa.Uuid(), nullable=True),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("request", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("response", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("truncated", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status in ('succeeded','failed')", name=op.f("ck_model_call_records_status_valid")
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_model_call_records_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["workflow_runs.id"],
            name=op.f("fk_model_call_records_run_id_workflow_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["step_id"],
            ["workflow_steps.id"],
            name=op.f("fk_model_call_records_step_id_workflow_steps"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["step_attempts.id"],
            name=op.f("fk_model_call_records_attempt_id_step_attempts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["agent_version_id"],
            ["agent_config_versions.id"],
            name=op.f("fk_model_call_records_agent_version_id_agent_config_versions"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_model_call_records")),
    )
    op.create_index("ix_model_call_records_run", "model_call_records", ["run_id", "created_at"])
    op.create_index("ix_model_call_records_step", "model_call_records", ["step_id"])
    # Recorded model traffic is evidence: it cannot be rewritten (agentos_forbid_change comes from 0002).
    op.execute(
        "CREATE TRIGGER model_call_records_immutable BEFORE UPDATE OR DELETE ON model_call_records "
        "FOR EACH ROW EXECUTE FUNCTION agentos_forbid_change()"
    )
    op.add_column("events", sa.Column("span_id", sa.String(length=16), nullable=True))
    op.add_column("workflow_runs", sa.Column("replay_of_run_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_workflow_runs_replay_of_run_id_workflow_runs"),
        "workflow_runs",
        "workflow_runs",
        ["replay_of_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.drop_constraint(op.f(PURPOSE), "workflow_runs", type_="check")
    op.create_check_constraint(
        op.f(PURPOSE), "workflow_runs", "purpose in ('workflow','evaluation','replay')"
    )


def downgrade() -> None:
    # Fails (by design) if replay runs exist: history is never rewritten.
    op.drop_constraint(op.f(PURPOSE), "workflow_runs", type_="check")
    op.create_check_constraint(op.f(PURPOSE), "workflow_runs", "purpose in ('workflow','evaluation')")
    op.drop_constraint(
        op.f("fk_workflow_runs_replay_of_run_id_workflow_runs"), "workflow_runs", type_="foreignkey"
    )
    op.drop_column("workflow_runs", "replay_of_run_id")
    op.drop_column("events", "span_id")
    op.execute("DROP TRIGGER IF EXISTS model_call_records_immutable ON model_call_records")
    op.drop_index("ix_model_call_records_step", table_name="model_call_records")
    op.drop_index("ix_model_call_records_run", table_name="model_call_records")
    op.drop_table("model_call_records")
