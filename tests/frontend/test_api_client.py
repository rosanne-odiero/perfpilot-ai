from __future__ import annotations

import httpx
import pytest

from frontend import api_client
from frontend.api_client import ENDPOINT_MISSING, UNREACHABLE, ApiError, Unsupported


def _response(status: int, *, json: object = None, text: str | None = None) -> httpx.Response:
    request = httpx.Request("GET", "http://api/api/audits/x/recommendations")
    if text is not None:
        return httpx.Response(status, request=request, text=text)
    return httpx.Response(status, request=request, json=json)


def test_error_envelope_is_parsed() -> None:
    error = api_client.error_from_response(
        _response(
            413,
            json={"error": {"code": "limit_exceeded", "message": "too big", "details": {"n": 1}}},
        )
    )
    assert (error.status, error.code, error.message, error.details) == (
        413,
        "limit_exceeded",
        "too big",
        {"n": 1},
    )
    assert not error.endpoint_missing


def test_unknown_route_is_reported_as_missing_endpoint() -> None:
    error = api_client.error_from_response(_response(404, json={"detail": "Not Found"}))
    assert error.code == ENDPOINT_MISSING
    assert error.endpoint_missing


def test_method_not_allowed_means_missing_endpoint() -> None:
    error = api_client.error_from_response(_response(405, json={"detail": "Method Not Allowed"}))
    assert error.endpoint_missing


def test_missing_resource_is_not_a_missing_endpoint() -> None:
    error = api_client.error_from_response(
        _response(404, json={"error": {"code": "not_found", "message": "audit x not found"}})
    )
    assert error.code == "not_found"
    assert not error.endpoint_missing


def test_non_json_error_keeps_a_short_body() -> None:
    error = api_client.error_from_response(_response(502, text="<html>" + "x" * 1000))
    assert error.code == "http_error"
    assert len(error.message) == 300


def test_get_if_supported_returns_unsupported_only_for_missing_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(*_: object, **__: object) -> object:
        raise ApiError(404, ENDPOINT_MISSING, "gone")

    monkeypatch.setattr(api_client, "request", missing)
    assert api_client.get_if_supported("/ai/status") == Unsupported("/ai/status")

    def not_found(*_: object, **__: object) -> object:
        raise ApiError(404, "not_found", "no such audit")

    monkeypatch.setattr(api_client, "request", not_found)
    with pytest.raises(ApiError):
        api_client.get_if_supported("/audits/x/recommendations")


def test_endpoint_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    def resource_missing(*_: object, **__: object) -> object:
        raise ApiError(404, "not_found", "optimization x not found")

    monkeypatch.setattr(api_client, "request", resource_missing)
    assert api_client.endpoint_available("/optimizations/probe") is True

    def route_missing(*_: object, **__: object) -> object:
        raise ApiError(404, ENDPOINT_MISSING, "gone")

    monkeypatch.setattr(api_client, "request", route_missing)
    assert api_client.endpoint_available("/optimizations/probe") is False


def test_unreachable_api_raises_typed_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERFPILOT_API_BASE_URL", "http://127.0.0.1:9")
    with pytest.raises(ApiError) as caught:
        api_client.get("/health")
    assert caught.value.code == UNREACHABLE
    assert "127.0.0.1:9" in caught.value.message


def test_download_filename_from_content_disposition() -> None:
    assert api_client._filename('attachment; filename="audit.pdf"') == "audit.pdf"
    assert api_client._filename(None) is None
