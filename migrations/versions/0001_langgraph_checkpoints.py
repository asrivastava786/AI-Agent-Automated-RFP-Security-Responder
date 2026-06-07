"""LangGraph checkpoint tables + application indexes

Revision ID: 0001
Revises:
Create Date: 2026-06-07 00:00:00.000000 UTC

Why manual migration instead of alembic autogenerate?
──────────────────────────────────────────────────────
LangGraph's AsyncPostgresSaver creates these tables via its own `.setup()`
method.  We replicate the DDL here so that:

  1. The schema is version-controlled alongside application code.
  2. `alembic upgrade head` is the single command that brings a fresh
     Postgres instance to the correct state (replaces calling `.setup()`
     on every app startup in production).
  3. Future schema changes (new columns, indexes, custom tables) have a
     clear migration chain to follow.

After applying this migration, set checkpointer.setup() to a no-op or
guard it with `IF NOT EXISTS` (it already is idempotent – but this migration
makes the explicit authoritative version).
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic
revision: str = "0001"
down_revision: str | None = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── LangGraph core checkpoint tables ─────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS checkpoints (
            thread_id     TEXT        NOT NULL,
            checkpoint_ns TEXT        NOT NULL DEFAULT '',
            checkpoint_id TEXT        NOT NULL,
            parent_checkpoint_id TEXT,
            type          TEXT,
            checkpoint     JSONB       NOT NULL DEFAULT '{}',
            metadata       JSONB       NOT NULL DEFAULT '{}',
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS checkpoint_blobs (
            thread_id      TEXT  NOT NULL,
            checkpoint_ns  TEXT  NOT NULL DEFAULT '',
            channel        TEXT  NOT NULL,
            version        TEXT  NOT NULL,
            type           TEXT  NOT NULL,
            blob           BYTEA,
            PRIMARY KEY (thread_id, checkpoint_ns, channel, version)
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS checkpoint_writes (
            thread_id      TEXT    NOT NULL,
            checkpoint_ns  TEXT    NOT NULL DEFAULT '',
            checkpoint_id  TEXT    NOT NULL,
            task_id        TEXT    NOT NULL,
            idx            INTEGER NOT NULL,
            channel        TEXT    NOT NULL,
            type           TEXT,
            blob           BYTEA   NOT NULL,
            PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
        )
    """)

    # ── Indexes for common access patterns ────────────────────────────────────

    # Latest checkpoint per thread: used by every aget_state() call
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_checkpoints_thread_created
        ON checkpoints (thread_id, created_at DESC)
    """)

    # Tenant-scoped queries (metadata->>'tenant_id')
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_checkpoints_tenant
        ON checkpoints ((metadata->>'tenant_id'))
        WHERE metadata->>'tenant_id' IS NOT NULL
    """)

    # Status queries for monitoring / dashboards
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_checkpoints_workflow_status
        ON checkpoints ((checkpoint->>'workflow_status'))
        WHERE checkpoint->>'workflow_status' IS NOT NULL
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_checkpoints_workflow_status")
    op.execute("DROP INDEX IF EXISTS idx_checkpoints_tenant")
    op.execute("DROP INDEX IF EXISTS idx_checkpoints_thread_created")
    op.execute("DROP TABLE IF EXISTS checkpoint_writes")
    op.execute("DROP TABLE IF EXISTS checkpoint_blobs")
    op.execute("DROP TABLE IF EXISTS checkpoints")
