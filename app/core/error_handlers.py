"""Turns domain errors raised anywhere in the app into HTTP responses, in one place."""

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from app.common.errors import (
    BusinessRuleError,
    DomainError,
    InvalidInputError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitedError,
)

STATUS_BY_ERROR: dict[type[DomainError], int] = {
    NotFoundError: status.HTTP_404_NOT_FOUND,
    PermissionDeniedError: status.HTTP_403_FORBIDDEN,
    BusinessRuleError: status.HTTP_409_CONFLICT,
    InvalidInputError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    RateLimitedError: status.HTTP_429_TOO_MANY_REQUESTS,
}


async def _domain_error(_: Request, exc: Exception) -> JSONResponse:
    code = next(
        (code for cls, code in STATUS_BY_ERROR.items() if isinstance(exc, cls)),
        status.HTTP_400_BAD_REQUEST,
    )
    headers = {"Retry-After": str(exc.retry_after)} if isinstance(exc, RateLimitedError) else None
    content: dict[str, str] = {"detail": str(exc)}
    if isinstance(exc, DomainError) and exc.code is not None:
        content["code"] = exc.code
    return JSONResponse(status_code=code, content=content, headers=headers)


def add_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(DomainError, _domain_error)
