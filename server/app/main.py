"""Byfrost Coordination Server - FastAPI application.

Handles authentication, device registration, and pairing for the
Byfrost bridge. All task data flows peer-to-peer; this server never
sees prompts, code, or output.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.database import engine


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application startup and shutdown."""
    # Engine is created at import time; just store reference for disposal
    app.state.engine = engine
    yield
    await engine.dispose()


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    settings = get_settings()

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        lifespan=lifespan,
    )

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Health check
    @app.get("/health")
    async def health() -> dict:
        """Health check endpoint."""
        return {"status": "ok", "service": "byfrost-server"}

    # Routers (endpoints added in Tasks 1.3+)
    from app.auth.router import router as auth_router
    from app.devices.router import router as devices_router
    from app.pairing.router import router as pairing_router

    app.include_router(auth_router, prefix="/auth", tags=["auth"])
    app.include_router(devices_router, prefix="/devices", tags=["devices"])
    app.include_router(pairing_router, prefix="/pair", tags=["pairing"])

    return app


app = create_app()
