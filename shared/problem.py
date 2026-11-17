"""RFC 7807 Problem Details — единый формат ошибок для всех сервисов."""
from typing import Optional

from fastapi import FastAPI, Request
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
    """Бросай в коде вместо HTTPException, если хочешь отдать Problem."""

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


async def _problem_handler(request: Request, exc: ProblemException) -> JSONResponse:
    correlation_id = request.headers.get("X-Correlation-Id")
    problem = Problem(
        type=exc.type,
        title=exc.title,
        status=exc.status,
        detail=exc.detail,
        instance=str(request.url.path),
        correlationId=correlation_id,
    )
    return JSONResponse(
        status_code=exc.status,
        content=problem.model_dump(exclude_none=True),
        media_type="application/problem+json",
    )


def install_problem_handler(app: FastAPI) -> None:
    app.add_exception_handler(ProblemException, _problem_handler)
