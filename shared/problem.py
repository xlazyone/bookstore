"""RFC 7807 Problem Details — единый формат ошибок для всех сервисов."""
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel


class Problem(BaseModel):
    type: str = "about:blank"
    title: str
    status: int
    detail: Optional[str] = None
    instance: Optional[str] = None
    correlationId: Optional[str] = None


class ProblemException(Exception):
    def __init__(
        self,
        status: int,
        title: str,
        detail: Optional[str] = None,
        type_: str = "about:blank",
    ):
        self.status = status
        self.title = title
        self.detail = detail
        self.type = type_


def _build_problem(request: Request, status: int, title: str, detail: Optional[str], type_: str) -> JSONResponse:
    correlation_id = request.headers.get("X-Correlation-Id")
    problem = Problem(
        type=type_,
        title=title,
        status=status,
        detail=detail,
        instance=str(request.url.path),
        correlationId=correlation_id,
    )
    return JSONResponse(
        status_code=status,
        content=problem.model_dump(exclude_none=True),
        media_type="application/problem+json",
    )


async def _problem_handler(request: Request, exc: ProblemException) -> JSONResponse:
    return _build_problem(request, exc.status, exc.title, exc.detail, exc.type)


async def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return _build_problem(
        request, 422, "Validation error", str(exc.errors())[:500],
        "https://bookstore.local/errors/validation",
    )


async def _http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail if isinstance(exc.detail, str) else None
    return _build_problem(
        request, exc.status_code, detail or "HTTP error", None,
        "https://bookstore.local/errors/http",
    )


def install_problem_handler(app: FastAPI) -> None:
    app.add_exception_handler(ProblemException, _problem_handler)
    app.add_exception_handler(RequestValidationError, _validation_handler)
    app.add_exception_handler(HTTPException, _http_exception_handler)
