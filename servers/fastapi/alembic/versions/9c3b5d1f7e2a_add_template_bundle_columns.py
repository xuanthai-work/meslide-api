"""add template bundle object-storage columns to template_v2

Revision ID: 9c3b5d1f7e2a
Revises: e5a7c9d1f3b4
Create Date: 2026-09-16 19:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "9c3b5d1f7e2a"
down_revision: Union[str, None] = "e5a7c9d1f3b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Nullable columns so legacy rows (JSON payloads in the old columns) keep
# working during the dual-read migration window; the legacy JSON columns are
# intentionally kept and dropped in a separate later release.
BUNDLE_COLUMNS = (
    ("bundle_object_key", sa.String),
    ("bundle_sha256", sa.String),
    ("bundle_size_bytes", sa.BigInteger),
    ("bundle_schema_version", sa.Integer),
    ("layout_count", sa.Integer),
)


def _table_exists(table_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table_name in inspector.get_table_names()


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return column_name in {
        column["name"] for column in inspector.get_columns(table_name)
    }


def upgrade() -> None:
    if not _table_exists("template_v2"):
        return
    for column_name, column_type in BUNDLE_COLUMNS:
        if not _has_column("template_v2", column_name):
            op.add_column(
                "template_v2",
                sa.Column(column_name, column_type(), nullable=True),
            )


def downgrade() -> None:
    if not _table_exists("template_v2"):
        return
    for column_name, _ in reversed(BUNDLE_COLUMNS):
        if _has_column("template_v2", column_name):
            op.drop_column("template_v2", column_name)