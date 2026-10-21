"""Initial AgentPal persistence and financial invariants."""

from pathlib import Path

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # One canonical PostgreSQL schema, including triggers, instead of a second
    # incomplete ORM definition. No autogeneration is used for native SQL.
    sql = Path(__file__).with_name("0001_persistence.sql").read_text()
    op.execute(sql)


def downgrade() -> None:
    # Explicit Alembic downgrade is destructive; never called at application startup.
    op.execute("DROP SCHEMA agentpal CASCADE")
