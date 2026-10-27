"""run traceparent: the root span context of a run, so worker spans join the trace that created it

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-26 22:39:46.204006
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("workflow_runs", sa.Column("traceparent", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("workflow_runs", "traceparent")
