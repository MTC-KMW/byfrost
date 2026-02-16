"""SQLAlchemy models for the coordination server."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class User(Base):
    """A user authenticated via GitHub OAuth."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    github_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    github_username: Mapped[str] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    devices: Mapped[list["Device"]] = relationship(back_populates="user")
    pairings: Mapped[list["Pairing"]] = relationship(back_populates="user")


class Device(Base):
    """A registered worker or controller device."""

    __tablename__ = "devices"
    __table_args__ = (Index("ix_devices_user_id", "user_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20))  # "worker" or "controller"
    platform: Mapped[str] = mapped_column(String(20))  # "macos", "linux", "windows"
    addresses: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    device_token: Mapped[str] = mapped_column(String(255))  # bcrypt hash
    last_heartbeat: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="devices")


class Pairing(Base):
    """A pairing between a worker and controller device."""

    __tablename__ = "pairings"
    __table_args__ = (Index("ix_pairings_user_id", "user_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    worker_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("devices.id", ondelete="CASCADE")
    )
    controller_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("devices.id", ondelete="CASCADE")
    )
    ca_cert: Mapped[str | None] = mapped_column(Text, nullable=True)
    worker_cert: Mapped[str | None] = mapped_column(Text, nullable=True)
    worker_key: Mapped[str | None] = mapped_column(Text, nullable=True)  # encrypted
    controller_cert: Mapped[str | None] = mapped_column(Text, nullable=True)
    controller_key: Mapped[str | None] = mapped_column(Text, nullable=True)  # encrypted
    hmac_secret: Mapped[str | None] = mapped_column(Text, nullable=True)
    prev_hmac_secret: Mapped[str | None] = mapped_column(Text, nullable=True)
    hmac_rotated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(String(20), server_default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="pairings")
    worker: Mapped["Device"] = relationship(foreign_keys=[worker_id])
    controller: Mapped["Device"] = relationship(foreign_keys=[controller_id])
