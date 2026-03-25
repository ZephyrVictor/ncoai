from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from ncoai_shared.bootstrap import ensure_application_schema


def create_app_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(database_url)


async def ensure_schema(database_url: str) -> None:
    await ensure_application_schema(database_url)
