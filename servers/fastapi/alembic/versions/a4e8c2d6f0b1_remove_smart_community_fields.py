"""remove Smart Mode and Community runtime fields

Revision ID: a4e8c2d6f0b1
Revises: 9c3b5d1f7e2a
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "a4e8c2d6f0b1"
down_revision: str | None = "9c3b5d1f7e2a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "presentations" in tables:
        presentation_columns = {
            column["name"] for column in inspector.get_columns("presentations")
        }
        if "generation_mode" in presentation_columns:
            smart_count = bind.execute(
                sa.text(
                    "SELECT COUNT(*) FROM presentations "
                    "WHERE generation_mode = 'smart'"
                )
            ).scalar_one()
            if smart_count:
                raise RuntimeError(
                    "Smart presentation data still exists; remove or migrate it "
                    "before dropping Smart Mode fields"
                )

    if "slides" in tables:
        slide_columns = {
            column["name"] for column in inspector.get_columns("slides")
        }
        if "html_content" in slide_columns:
            html_count = bind.execute(
                sa.text(
                    "SELECT COUNT(*) FROM slides "
                    "WHERE html_content IS NOT NULL "
                    "AND TRIM(html_content) <> ''"
                )
            ).scalar_one()
            if html_count:
                raise RuntimeError(
                    "Smart HTML slide data still exists; remove or migrate it "
                    "before dropping html_content"
                )

    if "presentations" in tables:
        presentation_columns = {
            column["name"] for column in inspector.get_columns("presentations")
        }
        removable = {"community_design_ids", "generation_mode"}
        if removable.intersection(presentation_columns):
            with op.batch_alter_table("presentations") as batch_op:
                if "community_design_ids" in presentation_columns:
                    batch_op.drop_column("community_design_ids")
                if "generation_mode" in presentation_columns:
                    batch_op.drop_column("generation_mode")

    if "slides" in tables:
        slide_columns = {
            column["name"] for column in inspector.get_columns("slides")
        }
        if "html_content" in slide_columns:
            with op.batch_alter_table("slides") as batch_op:
                batch_op.drop_column("html_content")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "presentations" in tables:
        presentation_columns = {
            column["name"] for column in inspector.get_columns("presentations")
        }
        with op.batch_alter_table("presentations") as batch_op:
            if "generation_mode" not in presentation_columns:
                batch_op.add_column(
                    sa.Column(
                        "generation_mode",
                        sa.String(),
                        nullable=False,
                        server_default="standard",
                    )
                )
            if "community_design_ids" not in presentation_columns:
                batch_op.add_column(
                    sa.Column("community_design_ids", sa.JSON(), nullable=True)
                )

    if "slides" in tables:
        slide_columns = {
            column["name"] for column in inspector.get_columns("slides")
        }
        if "html_content" not in slide_columns:
            with op.batch_alter_table("slides") as batch_op:
                batch_op.add_column(sa.Column("html_content", sa.String(), nullable=True))
