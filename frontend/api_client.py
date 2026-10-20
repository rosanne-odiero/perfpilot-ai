"""Thin typed client for the PerfPilot API, shared by all Streamlit pages.

This is the only module that talks to the backend; the UI never imports backend code or the DB.
Every failure surfaces as :class:`ApiError` carrying the API's error envelope
(``{"error": {"code", "message", "details"}}``) or a synthetic code for transport failures.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from email.message import Message
from typing import Any

import httpx

DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=5.0)
UPLOAD_TIMEOUT = httpx.Timeout(600.0, connect=5.0)

ENDPOINT_MISSING = "endpoint_not_found"
UNREACHABLE = "api_unreachable"
TIMEOUT = "api_timeout"


class ApiError(RuntimeError):
    def __init__(self, status: int, code: str, message: str, details: Any = None) -> None:
        super().__init__(f"[{status}] {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.details = details

    @property
    def endpoint_missing(self) -> bool:
        """The route itself does not exist in this API version (not a missing resource)."""
        return self.code == ENDPOINT_MISSING


@dataclass(frozen=True)
class Unsupported:
    """Returned by :func:`get_if_supported` when the server has no such endpoint."""

    path: str


@dataclass(frozen=True)
class Download:
    content: bytes
    filename: str | None
    media_type: str


def base_url() -> str:
    return os.environ.get("PERFPILOT_API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")


def error_from_response(response: httpx.Response) -> ApiError:
    try:
        body = response.json()
    except ValueError:
        body = None
    envelope = body.get("error") if isinstance(body, dict) else None
    if isinstance(envelope, dict):
        return ApiError(
            response.status_code,
            str(envelope.get("code", "http_error")),
            str(envelope.get("message", "")),
            envelope.get("details"),
        )
    # FastAPI answers unknown routes with a bare {"detail": "Not Found"} (no envelope) and
    # unknown methods on known routes with 405; both mean "this API version lacks the endpoint".
    if response.status_code == 405 or (
        response.status_code == 404 and isinstance(body, dict) and body.get("detail") == "Not Found"
    ):
        return ApiError(
            response.status_code,
            ENDPOINT_MISSING,
            f"{response.request.method} {response.request.url.path} is not available",
        )
    return ApiError(response.status_code, "http_error", response.text[:300])


def _send(method: str, path: str, timeout: httpx.Timeout, **kwargs: Any) -> httpx.Response:
    url = f"/api{path}"
    try:
        with httpx.Client(base_url=base_url(), timeout=timeout) as client:
            response = client.request(method, url, **kwargs)
    except httpx.TimeoutException as exc:
        raise ApiError(0, TIMEOUT, f"{method} {url} timed out ({base_url()})") from exc
    except httpx.TransportError as exc:
        raise ApiError(0, UNREACHABLE, f"cannot reach the API at {base_url()}: {exc}") from exc
    if not response.is_success:
        raise error_from_response(response)
    return response


def request(method: str, path: str, **kwargs: Any) -> Any:
    response = _send(method, path, DEFAULT_TIMEOUT, **kwargs)
    if response.headers.get("content-type", "").startswith("application/json"):
        return response.json()
    return response.content


def get(path: str, **kwargs: Any) -> Any:
    return request("GET", path, **kwargs)


def post(path: str, **kwargs: Any) -> Any:
    return request("POST", path, **kwargs)


def get_if_supported(path: str, **kwargs: Any) -> Any:
    """GET ``path``; return :class:`Unsupported` instead of raising when the route is missing."""
    try:
        return get(path, **kwargs)
    except ApiError as exc:
        if exc.endpoint_missing:
            return Unsupported(path)
        raise


def endpoint_available(path: str) -> bool:
    """Probe a parameterised GET route with a dummy id: a route-level 404/405 means absent."""
    try:
        get(path)
    except ApiError as exc:
        if exc.endpoint_missing:
            return False
        if exc.code in {UNREACHABLE, TIMEOUT}:
            raise
    return True


def upload_zip(filename: str, data: bytes, display_name: str | None = None) -> Any:
    fields = {"display_name": display_name} if display_name else None
    response = _send(
        "POST",
        "/repositories/upload",
        UPLOAD_TIMEOUT,
        files={"file": (filename, data, "application/zip")},
        data=fields,
    )
    return response.json()


def _filename(disposition: str | None) -> str | None:
    if not disposition:
        return None
    message = Message()
    message["content-disposition"] = disposition
    name = message.get_filename()
    return name or None


def download(path: str, **kwargs: Any) -> Download:
    response = _send("GET", path, DEFAULT_TIMEOUT, **kwargs)
    return Download(
        content=response.content,
        filename=_filename(response.headers.get("content-disposition")),
        media_type=response.headers.get("content-type", "application/octet-stream"),
    )
