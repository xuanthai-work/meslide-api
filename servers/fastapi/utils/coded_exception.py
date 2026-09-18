from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse


class CodedHTTPException(HTTPException):
    """HTTPException carrying a stable machine code for client localization.

    The legacy ``detail`` text stays verbatim for existing clients;
    ``error_code``/``error_params`` let localized clients map the error to a
    translated message instead of matching on English prose.
    """

    def __init__(
        self,
        status_code: int,
        detail: str | None = None,
        headers: Optional[dict] = None,
        *,
        error_code: Optional[str] = None,
        error_params: Optional[dict] = None,
    ):
        super().__init__(status_code=status_code, detail=detail, headers=headers)
        self.error_code = error_code
        self.error_params = error_params


async def coded_http_exception_handler(
    request: Request, exc: CodedHTTPException
) -> JSONResponse:
    payload = {"detail": exc.detail}
    if exc.error_code is not None:
        payload["error_code"] = exc.error_code
    if exc.error_params is not None:
        payload["error_params"] = exc.error_params
    return JSONResponse(
        status_code=exc.status_code,
        content=payload,
        headers=exc.headers,
    )


def register_coded_exception_handler(app: FastAPI) -> None:
    app.add_exception_handler(CodedHTTPException, coded_http_exception_handler)