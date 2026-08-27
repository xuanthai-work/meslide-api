from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Column, DateTime, ForeignKey, Uuid
from sqlmodel import Field, SQLModel

from utils.datetime_utils import get_current_utc_datetime


class UserProviderSettings(SQLModel, table=True):
    """Per-user provider configuration overlay.

    Each row holds the sanitized JSON the user saved from Settings. Provider
    getters first read this row for the current request, falling back to
    ``os.environ`` when the field is unset or blank. The table is keyed by
    ``user_id`` with cascade delete, so removing the user clears the overlay.
    """

    __tablename__ = "user_provider_settings"

    user_id: Any = Field(
        sa_column=Column(
            Uuid,
            ForeignKey("user.id", ondelete="CASCADE"),
            primary_key=True,
        )
    )
    config: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSON, nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=get_current_utc_datetime,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
