"""Harden payment lifecycle, capture accounting and budget-limit changes."""

from pathlib import Path

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(Path(__file__).with_name("0002_hardening.sql").read_text())


def downgrade() -> None:
    # Restores previous definitions only; does not delete or rewrite evidence.
    # It also restores the known 0001 guard weaknesses; do not use operationally.
    op.execute(Path(__file__).with_name("0002_hardening_down.sql").read_text())
