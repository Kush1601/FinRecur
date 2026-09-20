"""add superseded fix status

Revision ID: 8f1a2b3c4d5e
Revises: 2c80fdefe3d6
Create Date: 2026-09-20 09:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8f1a2b3c4d5e"
down_revision: str | Sequence[str] | None = "2c80fdefe3d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """edit_fix (spec 3.4 step 9) creates a new Fix version and marks the previous
    one superseded rather than deleting it -- the ledger is append-only."""
    op.execute("ALTER TYPE fix_status ADD VALUE IF NOT EXISTS 'SUPERSEDED'")


def downgrade() -> None:
    """Postgres has no DROP VALUE for enums; not worth rebuilding the type for a demo."""
