"""attempt reservations: release a dead attempt's unsettled model-call budget

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-26 21:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "step_attempts",
        sa.Column("reserved_usd", sa.Numeric(precision=14, scale=8), server_default="0", nullable=False),
    )
    # Reservations leaked by attempts that ended without settling (worker crashes before this fix) can only be
    # recovered for runs that have finished: give them back so finished runs report an accurate budget.
    op.execute(
        "UPDATE workflow_runs SET cost_reserved_usd = 0 WHERE status IN ('succeeded','failed','cancelled') "
        "AND cost_reserved_usd <> 0"
    )


def downgrade() -> None:
    op.drop_column("step_attempts", "reserved_usd")
