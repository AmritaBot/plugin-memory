"""state

迁移 ID: 6004d221a7de
父迁移: 21f55abc2b90
创建时间: 2026-09-25 15:20:42.537912

"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '6004d221a7de'
down_revision: str | Sequence[str] | None = '21f55abc2b90'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade(name: str = "") -> None:
    if name:
        return
    op.create_table('amrita_plugin_memory_subconscious_state',
    sa.Column('uid', sa.String(length=64), nullable=False),
    sa.Column('payload', sa.Text(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('uid', name=op.f('pk_amrita_plugin_memory_subconscious_state')),
    info={'bind_key': 'amrita_plugin_memory'}
    )


def downgrade(name: str = "") -> None:
    if name:
        return
    op.drop_table('amrita_plugin_memory_subconscious_state')
