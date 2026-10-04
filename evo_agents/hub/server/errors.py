"""Every error the API returns is JSON ``{error, message, request_id}``: a 404, a 405, a validation failure
or an unhandled exception never answers with HTML or a traceback.

Validation errors list where and why a request was rejected, without echoing the rejected input, which can
hold a credential.
"""

from __future__ import annotations

import logging
from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException

log = logging.getLogger(__name__)

CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "too_large",
    422: "invalid_request",
    429: "too_many_requests",
    500: "internal_error",
    502: "bad_gateway",  # GitHub did not answer, or answered with an error of its own
    503: "unavailable",
}


class ErrorBody(BaseModel):
    error: str  # a stable code such as not_found, for programs
    message: str  # for people
    request_id: str | None = None
    detail: list[dict] | None = None


def error_response(request: Request, status: int, message: str, *, detail=None, headers=None) -> JSONResponse:
    body = ErrorBody(
        error=CODES.get(status, f"http_{status}"),
        message=message,
        request_id=getattr(request.state, "request_id", None),
        detail=detail,
    )
    return JSONResponse(body.model_dump(exclude_none=True), status_code=status, headers=headers)


async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
    message = exc.detail if isinstance(exc.detail, str) else HTTPStatus(exc.status_code).phrase
    return error_response(request, exc.status_code, message, headers=getattr(exc, "headers", None))


async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    detail = [{"loc": list(e.get("loc", ())), "msg": e.get("msg", ""), "type": e.get("type", "")} for e in exc.errors()]
    return error_response(request, 422, "the request does not match the API", detail=detail)


async def internal_error(request: Request, exc: Exception) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None)
    log.error(
        "unhandled error",
        exc_info=exc,
        extra={"method": request.method, "path": request.url.path, "request_id": request_id},
    )
    return error_response(request, 500, "internal server error; quote the request_id when reporting it")


def install(app: FastAPI) -> None:
    app.add_exception_handler(HTTPException, http_error)
    app.add_exception_handler(RequestValidationError, validation_error)
    app.add_exception_handler(Exception, internal_error)
