"""tool execution replay count: evidence of same-attempt redundant calls answered from the recorded result

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-26 22:59:56.211758
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tool_executions", sa.Column("replay_count", sa.Integer(), server_default="0", nullable=False)
    )


def downgrade() -> None:
    op.drop_column("tool_executions", "replay_count")
