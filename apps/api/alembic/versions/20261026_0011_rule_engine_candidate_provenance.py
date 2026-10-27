"""rule engine: candidate provenance (rule id/version, type, evidence, projected flag)

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-26 21:10:37.178362
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("optimization_candidates", sa.Column("rule_id", sa.String(length=64), nullable=True))
    op.add_column("optimization_candidates", sa.Column("rule_version", sa.String(length=16), nullable=True))
    op.add_column("optimization_candidates", sa.Column("candidate_type", sa.String(length=32), nullable=True))
    op.add_column(
        "optimization_candidates",
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False),
    )
    op.add_column(
        "optimization_candidates",
        sa.Column("projected", sa.Boolean(), server_default="false", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("optimization_candidates", "projected")
    op.drop_column("optimization_candidates", "evidence")
    op.drop_column("optimization_candidates", "candidate_type")
    op.drop_column("optimization_candidates", "rule_version")
    op.drop_column("optimization_candidates", "rule_id")
