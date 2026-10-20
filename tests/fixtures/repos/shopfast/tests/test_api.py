from __future__ import annotations

import asyncio
import io
import json

import pytest

from shopfast import api
from shopfast.app import router


class _FakeResponse(io.BytesIO):
    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


@pytest.fixture
def fake_urlopen(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def urlopen(url: str, timeout: float) -> _FakeResponse:
        calls.append(url)
        currency = url.rsplit("=", 1)[-1]
        return _FakeResponse(json.dumps({"rate": len(currency) * 1.5}).encode())

    monkeypatch.setattr(api.urllib.request, "urlopen", urlopen)
    return calls


def test_get_product_handlers(products) -> None:
    sku = products[4].sku
    assert asyncio.run(api.get_product(products, sku)) == products[4]
    assert asyncio.run(api.get_product_polite(products, "missing")) is None


def test_router_dispatch(products) -> None:
    found = asyncio.run(router.dispatch("GET", "/products/{sku}", catalog=products, sku=products[0].sku))
    assert found == products[0]


def test_exchange_rates_use_injected_transport(fake_urlopen: list[str]) -> None:
    assert asyncio.run(api.fetch_exchange_rate("EUR")) == 4.5
    assert asyncio.run(api.fetch_exchange_rate_offloaded("KES")) == 4.5
    assert api.fetch_rates(["EUR", "GBP"]) == {"EUR": 4.5, "GBP": 4.5}
    assert len(fake_urlopen) == 4
