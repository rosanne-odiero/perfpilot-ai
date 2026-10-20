from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import streamlit as st
from ui_fixtures import FakeApi, load

from frontend import api_client


@pytest.fixture
def fake_api(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeApi]:
    fake = FakeApi()
    monkeypatch.setattr(api_client, "request", fake.request)
    monkeypatch.setattr(api_client, "download", fake.download)
    st.cache_data.clear()
    yield fake
    st.cache_data.clear()


@pytest.fixture
def audit_id() -> str:
    return next(a["id"] for a in load("audits.json") if a["status"] == "SUCCEEDED")


@pytest.fixture
def findings() -> list[dict[str, Any]]:
    manifest = load("manifest.json")
    key = next(k for k in manifest if k.endswith("/findings"))
    return list(load(manifest[key])["items"])


@pytest.fixture
def graph() -> dict[str, Any]:
    manifest = load("manifest.json")
    return dict(load(manifest[next(k for k in manifest if k.endswith("/graph"))]))
