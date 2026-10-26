"""allow the network-free 'fixture' provider on agent definitions

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-26 09:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NAME = "ck_agent_definitions_provider_valid"


def upgrade() -> None:
    op.drop_constraint(op.f(NAME), "agent_definitions", type_="check")
    op.create_check_constraint(
        op.f(NAME), "agent_definitions", "provider in ('anthropic','openai','mock','fixture')"
    )


def downgrade() -> None:
    # Fails (by design) if an agent still uses the fixture provider; history is never rewritten.
    op.drop_constraint(op.f(NAME), "agent_definitions", type_="check")
    op.create_check_constraint(op.f(NAME), "agent_definitions", "provider in ('anthropic','openai','mock')")
