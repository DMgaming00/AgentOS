"""memory lab: records, versions, audit log, experiments

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-26 20:18:25.158708
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "memory_experiments",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "config",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("results", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status in ('running','completed','failed','cancelled')",
            name=op.f("ck_memory_experiments_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_memory_experiments_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_memory_experiments_workspace_id_workspaces"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memory_experiments")),
    )
    op.create_index(
        "ix_memory_experiments_ws", "memory_experiments", ["workspace_id", "created_at"], unique=False
    )
    op.create_table(
        "memory_records",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("namespace", sa.String(length=100), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("key", sa.String(length=200), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("tags", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False),
        sa.Column(
            "source",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_reason", sa.String(length=16), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("updated_by", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english', coalesce(key,'') || ' ' || content)", persisted=True),
            nullable=False,
        ),
        sa.CheckConstraint(
            "deleted_reason is null or deleted_reason in ('deleted','expired')",
            name=op.f("ck_memory_records_deleted_reason_valid"),
        ),
        sa.CheckConstraint("kind in ('kv','note','task_summary')", name=op.f("ck_memory_records_kind_valid")),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_memory_records_created_by_users"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"], ["users.id"], name=op.f("fk_memory_records_updated_by_users"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_memory_records_workspace_id_workspaces"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memory_records")),
    )
    op.create_index(
        "ix_memory_records_search", "memory_records", ["search_vector"], unique=False, postgresql_using="gin"
    )
    op.create_index(
        "ix_memory_records_ws_ns", "memory_records", ["workspace_id", "namespace", "kind"], unique=False
    )
    op.create_index(
        "uq_memory_records_live_key",
        "memory_records",
        ["workspace_id", "namespace", "key"],
        unique=True,
        postgresql_where=sa.text("key is not null and deleted_at is null"),
    )
    op.create_table(
        "memory_audit_events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("record_id", sa.UUID(), nullable=True),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("actor_type", sa.String(length=16), nullable=False),
        sa.Column("actor_id", sa.String(length=100), nullable=True),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "action in ('create','read','search','update','delete','expire','denied')",
            name=op.f("ck_memory_audit_events_action_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["record_id"],
            ["memory_records.id"],
            name=op.f("fk_memory_audit_events_record_id_memory_records"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_memory_audit_events_workspace_id_workspaces"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memory_audit_events")),
    )
    op.create_index("ix_memory_audit_record", "memory_audit_events", ["record_id"], unique=False)
    op.create_index(
        "ix_memory_audit_ws_created", "memory_audit_events", ["workspace_id", "created_at"], unique=False
    )
    op.create_table(
        "memory_versions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("record_id", sa.UUID(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("change_type", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("tags", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("changed_by", sa.String(length=100), nullable=True),
        sa.Column("changed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "change_type in ('create','update','delete','expire')",
            name=op.f("ck_memory_versions_change_type_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["record_id"],
            ["memory_records.id"],
            name=op.f("fk_memory_versions_record_id_memory_records"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memory_versions")),
        sa.UniqueConstraint("record_id", "version", "change_type", name="uq_memory_versions_identity"),
    )
    op.create_index("ix_memory_versions_record", "memory_versions", ["record_id", "version"], unique=False)
    # Version history and the audit log are append-only; a finished experiment is sealed.
    for table in ("memory_versions", "memory_audit_events"):
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION agentos_forbid_change()"
        )
    op.execute(
        "CREATE TRIGGER memory_experiments_sealed BEFORE UPDATE OR DELETE ON memory_experiments "
        "FOR EACH ROW EXECUTE FUNCTION agentos_seal_completed()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS memory_experiments_sealed ON memory_experiments")
    op.execute("DROP TRIGGER IF EXISTS memory_audit_events_immutable ON memory_audit_events")
    op.execute("DROP TRIGGER IF EXISTS memory_versions_immutable ON memory_versions")
    op.drop_index("ix_memory_versions_record", table_name="memory_versions")
    op.drop_table("memory_versions")
    op.drop_index("ix_memory_audit_ws_created", table_name="memory_audit_events")
    op.drop_index("ix_memory_audit_record", table_name="memory_audit_events")
    op.drop_table("memory_audit_events")
    op.drop_index(
        "uq_memory_records_live_key",
        table_name="memory_records",
        postgresql_where=sa.text("key is not null and deleted_at is null"),
    )
    op.drop_index("ix_memory_records_ws_ns", table_name="memory_records")
    op.drop_index("ix_memory_records_search", table_name="memory_records", postgresql_using="gin")
    op.drop_table("memory_records")
    op.drop_index("ix_memory_experiments_ws", table_name="memory_experiments")
    op.drop_table("memory_experiments")
