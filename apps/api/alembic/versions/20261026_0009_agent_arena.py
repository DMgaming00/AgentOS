"""agent arena: arena runs and entries

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-26 22:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONB = postgresql.JSONB(astext_type=sa.Text())
EMPTY = sa.text("'{}'::jsonb")


def upgrade() -> None:
    op.create_table(
        "arena_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("config", JSONB, server_default=EMPTY, nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("final_test_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("final_test_by", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_arena_runs_workspace_id_workspaces"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_arena_runs_created_by_users"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["final_test_by"],
            ["users.id"],
            name=op.f("fk_arena_runs_final_test_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_arena_runs")),
    )
    op.create_index("ix_arena_runs_ws_created", "arena_runs", ["workspace_id", "created_at"])
    op.create_table(
        "arena_entries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("arena_run_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("agent_version_id", sa.Uuid(), nullable=False),
        sa.Column("dataset_id", sa.Uuid(), nullable=False),
        sa.Column("family", sa.String(length=32), nullable=False),
        sa.Column("split", sa.String(length=16), nullable=False),
        sa.Column("evaluation_run_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "split in ('development','validation','test')", name=op.f("ck_arena_entries_split_valid")
        ),
        sa.ForeignKeyConstraint(
            ["arena_run_id"],
            ["arena_runs.id"],
            name=op.f("fk_arena_entries_arena_run_id_arena_runs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agent_definitions.id"],
            name=op.f("fk_arena_entries_agent_id_agent_definitions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["agent_version_id"],
            ["agent_config_versions.id"],
            name=op.f("fk_arena_entries_agent_version_id_agent_config_versions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["dataset_id"],
            ["evaluation_datasets.id"],
            name=op.f("fk_arena_entries_dataset_id_evaluation_datasets"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["evaluation_run_id"],
            ["evaluation_runs.id"],
            name=op.f("fk_arena_entries_evaluation_run_id_evaluation_runs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_arena_entries")),
        sa.UniqueConstraint(
            "arena_run_id", "agent_version_id", "dataset_id", name="uq_arena_entries_identity"
        ),
    )
    op.create_index("ix_arena_entries_run", "arena_entries", ["arena_run_id"])
    op.create_index("ix_arena_entries_eval", "arena_entries", ["evaluation_run_id"])


def downgrade() -> None:
    op.drop_table("arena_entries")
    op.drop_table("arena_runs")
