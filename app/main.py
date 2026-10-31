from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

import app.modules.registry  # noqa: F401
from app.core.db import engine, get_session
from app.modules.identity.router import router as auth_router
from app.modules.orders.router import orders_router, registrations_router, tickets_router


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    engine.dispose()


app = FastAPI(title="ticketoff API", lifespan=lifespan)
app.include_router(auth_router)
app.include_router(registrations_router)
app.include_router(orders_router)
app.include_router(tickets_router)


@app.get("/health", tags=["health"])
def health(session: Annotated[Session, Depends(get_session)]) -> JSONResponse:
    try:
        session.execute(text("SELECT 1"))
    except (SQLAlchemyError, OSError):
        return JSONResponse(status_code=503, content={"status": "error", "database": "unavailable"})
    return JSONResponse(content={"status": "ok", "database": "ok"})
