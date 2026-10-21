"""Recheck new human decisions at commit, preserving immutable history."""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Reuse the authoritative snapshot/policy/session/expiry/dispatch guard.
    # INSERT-time validation alone permits expiry during a later transaction wait.
    op.execute("""CREATE CONSTRAINT TRIGGER approval_commit_guard
        AFTER INSERT ON agentpal.approvals DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION agentpal.validate_approval()""")


def downgrade() -> None:
    # Definitions only; no existing decisions or audit history are rewritten.
    op.execute("DROP TRIGGER approval_commit_guard ON agentpal.approvals")
