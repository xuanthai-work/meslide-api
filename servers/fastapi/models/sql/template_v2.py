from datetime import datetime
from typing import Optional
import uuid

from sqlmodel import Field, SQLModel
from sqlalchemy import JSON, BigInteger, Column, DateTime, ForeignKey, Integer, String

from utils.datetime_utils import get_current_utc_datetime
from api.v1.auth.context import get_current_owner_id


def _new_template_v2_id() -> str:
    return str(uuid.uuid4())


class TemplateV2(SQLModel, table=True):
    __tablename__ = "template_v2"

    id: str = Field(primary_key=True, default_factory=_new_template_v2_id)
    owner_id: Optional[uuid.UUID] = Field(
        default_factory=get_current_owner_id,
        exclude=True,
        sa_column=Column(
            ForeignKey("user.id", ondelete="CASCADE"), nullable=True, index=True
        ),
    )
    name: str = Field(nullable=False)
    description: Optional[str] = Field(default=None, nullable=True)
    raw_layouts: Optional[dict] = Field(
        default=None, sa_column=Column(JSON, nullable=True)
    )
    components: Optional[dict] = Field(
        default=None, sa_column=Column(JSON, nullable=True)
    )
    merged_components: Optional[dict] = Field(
        default=None, sa_column=Column(JSON, nullable=True)
    )
    layouts: Optional[dict] = Field(
        default=None, sa_column=Column(JSON, nullable=True)
    )
    assets: Optional[dict] = Field(
        default=None, sa_column=Column(JSON, nullable=True)
    )
    is_default: bool = Field(default=False, nullable=False)
    # Object-storage reference for the template bundle (S3/MinIO migration).
    # Rows predating the migration keep the legacy JSON columns above only;
    # bundle columns stay null until a bundle is written or backfilled.
    bundle_object_key: Optional[str] = Field(
        default=None, sa_column=Column(String, nullable=True)
    )
    bundle_sha256: Optional[str] = Field(
        default=None, sa_column=Column(String, nullable=True)
    )
    bundle_size_bytes: Optional[int] = Field(
        default=None, sa_column=Column(BigInteger, nullable=True)
    )
    bundle_schema_version: Optional[int] = Field(
        default=None, sa_column=Column(Integer, nullable=True)
    )
    layout_count: Optional[int] = Field(
        default=None, sa_column=Column(Integer, nullable=True)
    )
    created_at: datetime = Field(
        sa_column=Column(
            DateTime(timezone=True), nullable=False, default=get_current_utc_datetime
        )
    )
    updated_at: datetime = Field(
        sa_column=Column(
            DateTime(timezone=True),
            nullable=False,
            default=get_current_utc_datetime,
            onupdate=get_current_utc_datetime,
        )
    )
