"""add rescued cluster kind

Revision ID: 2c80fdefe3d6
Revises: 3acb6c07a86d
Create Date: 2026-09-20 07:14:52.704783

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2c80fdefe3d6"
down_revision: str | Sequence[str] | None = "3acb6c07a86d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema. Postgres allows adding an enum value inside a transaction as
    long as the new value isn't used in that same transaction (spec 3.4 step 4a: a
    grouping strategy produces a `rescued` cluster kind, distinct from
    escalated/absorbed/mixed)."""
    # every other enum in this schema stores the Python Enum member's *name*
    # (SQLAlchemy's default), e.g. "ESCALATED" not "escalated" -- match that.
    op.execute("ALTER TYPE cluster_kind ADD VALUE IF NOT EXISTS 'RESCUED'")


def downgrade() -> None:
    """Postgres has no DROP VALUE for enums; downgrading this one requires rebuilding
    the type, which isn't worth it for a demo. No-op."""
