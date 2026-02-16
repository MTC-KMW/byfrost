"""Async SQLAlchemy database engine and session management."""

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings

engine = create_async_engine(get_settings().database_url, echo=False)
async_session = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy models."""


async def get_db() -> AsyncIterator[AsyncSession]:
    """Yield a database session for FastAPI dependency injection."""
    async with async_session() as session:
        yield session
