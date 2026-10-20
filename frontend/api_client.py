"""Thin typed client for the PerfPilot API, shared by all Streamlit pages."""

from __future__ import annotations

import os
from typing import Any

import httpx

DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=5.0)


class ApiError(RuntimeError):
    def __init__(self, status: int, code: str, message: str, details: Any = None) -> None:
        super().__init__(f"[{status}] {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.details = details


def base_url() -> str:
    return os.environ.get("PERFPILOT_API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")


def _raise_for(response: httpx.Response) -> None:
    if response.is_success:
        return
    try:
        err = response.json().get("error", {})
    except ValueError:
        err = {}
    raise ApiError(
        response.status_code,
        err.get("code", "http_error"),
        err.get("message", response.text[:300]),
        err.get("details"),
    )


def request(method: str, path: str, **kwargs: Any) -> Any:
    with httpx.Client(base_url=base_url(), timeout=DEFAULT_TIMEOUT) as client:
        response = client.request(method, f"/api{path}", **kwargs)
    _raise_for(response)
    if response.headers.get("content-type", "").startswith("application/json"):
        return response.json()
    return response.content


def get(path: str, **kwargs: Any) -> Any:
    return request("GET", path, **kwargs)


def post(path: str, **kwargs: Any) -> Any:
    return request("POST", path, **kwargs)
