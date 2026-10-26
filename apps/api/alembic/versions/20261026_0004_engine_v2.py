"""engine v2: run priority, pause/resume, nested workflows, waiting_child step state

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-26 11:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STEP_STATUS = "ck_workflow_steps_status_valid"
OLD = (
    "status in ('pending','ready','running','waiting_approval','retry_wait',"
    "'succeeded','failed','skipped','cancelled')"
)
NEW = (
    "status in ('pending','ready','running','waiting_approval','retry_wait','waiting_child',"
    "'succeeded','failed','skipped','cancelled')"
)


def upgrade() -> None:
    op.add_column(
        "workflow_runs", sa.Column("priority", sa.SmallInteger(), server_default="0", nullable=False)
    )
    op.add_column("workflow_runs", sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("workflow_runs", sa.Column("pause_reason", sa.String(length=500), nullable=True))
    op.add_column("workflow_runs", sa.Column("parent_run_id", sa.Uuid(), nullable=True))
    op.add_column("workflow_runs", sa.Column("parent_step_id", sa.Uuid(), nullable=True))
    op.add_column("workflow_runs", sa.Column("depth", sa.SmallInteger(), server_default="0", nullable=False))
    op.create_foreign_key(
        op.f("fk_workflow_runs_parent_run_id_workflow_runs"),
        "workflow_runs",
        "workflow_runs",
        ["parent_run_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_workflow_runs_parent_step",
        "workflow_runs",
        "workflow_steps",
        ["parent_step_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_check_constraint(
        op.f("ck_workflow_runs_priority_range"), "workflow_runs", "priority between 0 and 9"
    )
    op.create_check_constraint(op.f("ck_workflow_runs_depth_range"), "workflow_runs", "depth between 0 and 5")
    op.create_index(
        "ix_workflow_runs_parent",
        "workflow_runs",
        ["parent_run_id"],
        postgresql_where=sa.text("parent_run_id is not null"),
    )
    op.drop_constraint(op.f(STEP_STATUS), "workflow_steps", type_="check")
    op.create_check_constraint(op.f(STEP_STATUS), "workflow_steps", NEW)


def downgrade() -> None:
    # Fails (by design) if a step is waiting_child: history is never rewritten.
    op.drop_constraint(op.f(STEP_STATUS), "workflow_steps", type_="check")
    op.create_check_constraint(op.f(STEP_STATUS), "workflow_steps", OLD)
    op.drop_index("ix_workflow_runs_parent", table_name="workflow_runs")
    op.drop_constraint(op.f("ck_workflow_runs_depth_range"), "workflow_runs", type_="check")
    op.drop_constraint(op.f("ck_workflow_runs_priority_range"), "workflow_runs", type_="check")
    op.drop_constraint("fk_workflow_runs_parent_step", "workflow_runs", type_="foreignkey")
    op.drop_constraint(
        op.f("fk_workflow_runs_parent_run_id_workflow_runs"), "workflow_runs", type_="foreignkey"
    )
    for col in ("depth", "parent_step_id", "parent_run_id", "pause_reason", "paused_at", "priority"):
        op.drop_column("workflow_runs", col)
