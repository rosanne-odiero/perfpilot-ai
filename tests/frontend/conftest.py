from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import streamlit as st
from ui_fixtures import FakeApi, load

from frontend import api_client


def _install(monkeypatch: pytest.MonkeyPatch, fake: FakeApi) -> Iterator[FakeApi]:
    monkeypatch.setattr(api_client, "request", fake.request)
    monkeypatch.setattr(api_client, "download", fake.download)
    st.cache_data.clear()
    yield fake
    st.cache_data.clear()


@pytest.fixture
def fake_api(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeApi]:
    yield from _install(monkeypatch, FakeApi())


@pytest.fixture
def legacy_api(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeApi]:
    """An API version without the AI, runtime and optimization endpoints."""
    yield from _install(monkeypatch, FakeApi(legacy=True))


@pytest.fixture
def audit_id() -> str:
    """The captured RUNTIME audit (with a measured run) whose sub-resources are recorded."""
    key = next(k for k in load("manifest.json") if k.endswith("/findings"))
    return key.split("/")[2]


@pytest.fixture
def findings() -> list[dict[str, Any]]:
    manifest = load("manifest.json")
    key = next(k for k in manifest if k.endswith("/findings"))
    return list(load(manifest[key])["items"])


@pytest.fixture
def graph() -> dict[str, Any]:
    manifest = load("manifest.json")
    return dict(load(manifest[next(k for k in manifest if k.endswith("/graph"))]))
