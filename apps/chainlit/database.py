from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from schema import CHAINLIT_SCHEMA_STATEMENTS


def create_app_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(database_url)


async def ensure_chainlit_schema(database_url: str) -> None:
    engine = create_app_engine(database_url)
    try:
        async with engine.begin() as conn:
            for statement in CHAINLIT_SCHEMA_STATEMENTS:
                await conn.execute(text(statement))
    finally:
        await engine.dispose()
