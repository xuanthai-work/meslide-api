"""per-user provider settings overlay + drop admin columns

Revision ID: e5a7c9d1f3b4
Revises: d2f4a6b8c0e1

Adds ``user_provider_settings`` (one row per user) and drops the legacy
``user.admin_slot`` index/column plus ``user.is_superuser``. The new model has
no admin persona: every authenticated user can save their own LLM/image/search
keys, with process env as fallback until they do.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "e5a7c9d1f3b4"
down_revision: str | None = "d2f4a6b8c0e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "user_provider_settings" not in tables:
        op.create_table(
            "user_provider_settings",
            sa.Column("user_id", sa.Uuid(), nullable=False),
            sa.Column("config", sa.JSON(), nullable=False),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(
                ["user_id"],
                ["user.id"],
                name="fk_user_provider_settings_user_id",
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("user_id"),
        )

    if "user" not in tables:
        return

    indexes = {index["name"] for index in inspector.get_indexes("user")}
    columns = {column["name"] for column in inspector.get_columns("user")}

    with op.batch_alter_table("user") as batch:
        if "uq_user_admin_slot" in indexes:
            batch.drop_index("uq_user_admin_slot")
        if "admin_slot" in columns:
            batch.drop_column("admin_slot")
        if "is_superuser" in columns:
            batch.drop_column("is_superuser")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "user" in inspector.get_table_names():
        columns = {column["name"] for column in inspector.get_columns("user")}
        with op.batch_alter_table("user") as batch:
            if "is_superuser" not in columns:
                batch.add_column(
                    sa.Column(
                        "is_superuser",
                        sa.Boolean(),
                        nullable=False,
                        server_default=sa.text("false"),
                    )
                )
            if "admin_slot" not in columns:
                batch.add_column(
                    sa.Column("admin_slot", sa.String(length=32), nullable=True)
                )
        indexes = {index["name"] for index in sa.inspect(bind).get_indexes("user")}
        if "uq_user_admin_slot" not in indexes:
            op.create_index(
                "uq_user_admin_slot",
                "user",
                ["admin_slot"],
                unique=True,
            )

    tables = set(inspector.get_table_names())
    if "user_provider_settings" in tables:
        op.drop_table("user_provider_settings")
