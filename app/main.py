from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import engine, get_session


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    await engine.dispose()


app = FastAPI(title="ticketoff API", lifespan=lifespan)


@app.get("/health", tags=["health"])
async def health(session: Annotated[AsyncSession, Depends(get_session)]) -> JSONResponse:
    try:
        await session.execute(text("SELECT 1"))
    except (SQLAlchemyError, OSError):
        return JSONResponse(status_code=503, content={"status": "error", "database": "unavailable"})
    return JSONResponse(content={"status": "ok", "database": "ok"})
