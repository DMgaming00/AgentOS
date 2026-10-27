"""finops: budgets (workspace/agent, daily/monthly) and cost anomalies

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-26 21:20:34.290056
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "finops_anomalies",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("severity", sa.String(length=8), nullable=False),
        sa.Column("subject", sa.String(length=200), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column(
            "evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("acknowledged_by", sa.UUID(), nullable=True),
        sa.CheckConstraint(
            "severity in ('info','warning','high')", name=op.f("ck_finops_anomalies_severity_valid")
        ),
        sa.CheckConstraint(
            "status in ('open','acknowledged')", name=op.f("ck_finops_anomalies_status_valid")
        ),
        sa.ForeignKeyConstraint(
            ["acknowledged_by"],
            ["users.id"],
            name=op.f("fk_finops_anomalies_acknowledged_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_finops_anomalies_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_finops_anomalies")),
        sa.UniqueConstraint("workspace_id", "fingerprint", name="uq_finops_anomalies_fingerprint"),
    )
    op.create_index(
        "ix_finops_anomalies_ws", "finops_anomalies", ["workspace_id", "detected_at"], unique=False
    )
    op.create_table(
        "finops_budgets",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("scope", sa.String(length=12), nullable=False),
        sa.Column("agent_id", sa.UUID(), nullable=True),
        sa.Column("period", sa.String(length=8), nullable=False),
        sa.Column("limit_usd", sa.Numeric(precision=14, scale=6), nullable=False),
        sa.Column("mode", sa.String(length=8), nullable=False),
        sa.Column("warn_fraction", sa.Numeric(precision=4, scale=3), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "(scope = 'agent') = (agent_id is not null)", name=op.f("ck_finops_budgets_agent_matches_scope")
        ),
        sa.CheckConstraint("mode in ('enforce','alert')", name=op.f("ck_finops_budgets_mode_valid")),
        sa.CheckConstraint("period in ('daily','monthly')", name=op.f("ck_finops_budgets_period_valid")),
        sa.CheckConstraint("scope in ('workspace','agent')", name=op.f("ck_finops_budgets_scope_valid")),
        sa.CheckConstraint("limit_usd > 0", name=op.f("ck_finops_budgets_limit_positive")),
        sa.CheckConstraint(
            "warn_fraction > 0 and warn_fraction <= 1", name=op.f("ck_finops_budgets_warn_fraction_range")
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agent_definitions.id"],
            name=op.f("fk_finops_budgets_agent_id_agent_definitions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_finops_budgets_created_by_users"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_finops_budgets_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_finops_budgets")),
    )
    op.create_index(
        "uq_finops_budgets_scope",
        "finops_budgets",
        [
            "workspace_id",
            "scope",
            sa.literal_column("coalesce(agent_id, '00000000-0000-0000-0000-000000000000')"),
            "period",
        ],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_finops_budgets_scope", table_name="finops_budgets")
    op.drop_table("finops_budgets")
    op.drop_index("ix_finops_anomalies_ws", table_name="finops_anomalies")
    op.drop_table("finops_anomalies")
