"""Точка входа HTTP API (контейнер app)."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import health
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
    return app


app = create_app()
