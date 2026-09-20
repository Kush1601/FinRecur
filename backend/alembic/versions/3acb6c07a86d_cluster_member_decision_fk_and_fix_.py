"""cluster member decision fk and fix proposal columns

Revision ID: 3acb6c07a86d
Revises: 0001_initial
Create Date: 2026-09-20 07:07:16.973389

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3acb6c07a86d"
down_revision: str | Sequence[str] | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("cluster_members", sa.Column("decision_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_cluster_members_decision_id", "cluster_members", "decisions", ["decision_id"], ["id"]
    )
    op.add_column(
        "fixes",
        sa.Column("proposed_by", sa.String(), nullable=False, server_default="agent"),
    )
    op.add_column(
        "fixes",
        sa.Column("is_alternative", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("fixes", "is_alternative")
    op.drop_column("fixes", "proposed_by")
    op.drop_constraint("fk_cluster_members_decision_id", "cluster_members", type_="foreignkey")
    op.drop_column("cluster_members", "decision_id")
