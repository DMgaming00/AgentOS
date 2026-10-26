"""security center: versioned test cases, evaluation runs and results, security run purpose

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-26 20:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PURPOSE = "ck_workflow_runs_purpose_valid"
JSONB = postgresql.JSONB(astext_type=sa.Text())
EMPTY = sa.text("'{}'::jsonb")


def upgrade() -> None:
    op.create_table(
        "security_test_cases",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("suite_version", sa.String(length=64), nullable=False),
        sa.Column("case_key", sa.String(length=200), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("expected", sa.Text(), nullable=False),
        sa.Column("definition", JSONB, server_default=EMPTY, nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_security_test_cases")),
        sa.UniqueConstraint("suite_version", "case_key", name="uq_security_test_cases_version_key"),
    )
    op.create_table(
        "security_evaluation_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("suite_version", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("phase", sa.String(length=24), nullable=False),
        sa.Column("conditions", JSONB, server_default=EMPTY, nullable=False),
        sa.Column("results", JSONB, nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status in ('running','completed','failed','cancelled')",
            name=op.f("ck_security_evaluation_runs_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_security_evaluation_runs_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_security_evaluation_runs_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_security_evaluation_runs")),
    )
    op.create_index(
        "ix_security_evaluation_runs_ws", "security_evaluation_runs", ["workspace_id", "created_at"]
    )
    op.create_table(
        "security_evaluation_results",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("evaluation_run_id", sa.Uuid(), nullable=False),
        sa.Column("case_key", sa.String(length=200), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("verdict", sa.String(length=8), nullable=False),
        sa.Column("observed", JSONB, server_default=EMPTY, nullable=False),
        sa.Column("evidence", JSONB, server_default=EMPTY, nullable=False),
        sa.Column("workflow_run_id", sa.Uuid(), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "verdict in ('pass','fail','error')", name=op.f("ck_security_evaluation_results_verdict_valid")
        ),
        sa.ForeignKeyConstraint(
            ["evaluation_run_id"],
            ["security_evaluation_runs.id"],
            name=op.f("fk_security_evaluation_results_evaluation_run_id_security_evaluation_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workflow_run_id"],
            ["workflow_runs.id"],
            name=op.f("fk_security_evaluation_results_workflow_run_id_workflow_runs"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_security_evaluation_results")),
        sa.UniqueConstraint("evaluation_run_id", "case_key", name="uq_security_results_run_case"),
    )
    op.create_index("ix_security_results_run", "security_evaluation_results", ["evaluation_run_id"])
    # Catalog rows are immutable (a changed case is a new suite version); finished runs are sealed;
    # results are append-only evidence. (Functions come from migration 0002.)
    op.execute(
        "CREATE TRIGGER security_test_cases_immutable BEFORE UPDATE OR DELETE ON security_test_cases "
        "FOR EACH ROW EXECUTE FUNCTION agentos_forbid_change()"
    )
    op.execute(
        "CREATE TRIGGER security_evaluation_runs_sealed BEFORE UPDATE OR DELETE ON security_evaluation_runs "
        "FOR EACH ROW EXECUTE FUNCTION agentos_seal_completed()"
    )
    op.drop_constraint(op.f(PURPOSE), "workflow_runs", type_="check")
    op.create_check_constraint(
        op.f(PURPOSE), "workflow_runs", "purpose in ('workflow','evaluation','replay','chaos','security')"
    )


def downgrade() -> None:
    op.drop_constraint(op.f(PURPOSE), "workflow_runs", type_="check")
    op.create_check_constraint(
        op.f(PURPOSE), "workflow_runs", "purpose in ('workflow','evaluation','replay','chaos')"
    )
    op.execute("DROP TRIGGER IF EXISTS security_evaluation_runs_sealed ON security_evaluation_runs")
    op.execute("DROP TRIGGER IF EXISTS security_test_cases_immutable ON security_test_cases")
    op.drop_index("ix_security_results_run", table_name="security_evaluation_results")
    op.drop_table("security_evaluation_results")
    op.drop_index("ix_security_evaluation_runs_ws", table_name="security_evaluation_runs")
    op.drop_table("security_evaluation_runs")
    op.drop_table("security_test_cases")
