"""Точка входа HTTP API (контейнер app)."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import admin, auth, documents, health, integration, manual_upload, sources
from app.config import get_settings
from app.db import dispose_engine


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    get_settings()  # падаем при старте, если конфигурация небезопасна (см. validate_prod)
    yield
    await dispose_engine()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Поиск по документам предприятия",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
    )
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(admin.router)
    app.include_router(sources.router)
    app.include_router(manual_upload.router)
    app.include_router(documents.router)
    app.include_router(integration.router)
    return app


app = create_app()
