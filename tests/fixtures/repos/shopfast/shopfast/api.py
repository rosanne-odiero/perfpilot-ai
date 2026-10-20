"""Async request handlers."""

from __future__ import annotations

import asyncio
import json
import time
import urllib.request

from shopfast.app import router
from shopfast.catalog import Product

RATE_LIMIT_SECONDS = 0.002
EXCHANGE_URL = "https://rates.example.invalid/latest?base={currency}"


@router.get("/products/{sku}")
async def get_product(catalog: list[Product], sku: str) -> Product | None:
    time.sleep(RATE_LIMIT_SECONDS)
    return next((p for p in catalog if p.sku == sku), None)


@router.get("/products/{sku}/polite")
async def get_product_polite(catalog: list[Product], sku: str) -> Product | None:
    await asyncio.sleep(RATE_LIMIT_SECONDS)
    return next((p for p in catalog if p.sku == sku), None)


@router.get("/rates/{currency}")
async def fetch_exchange_rate(currency: str) -> float:
    with urllib.request.urlopen(EXCHANGE_URL.format(currency=currency), timeout=5) as response:
        payload = json.load(response)
    return float(payload["rate"])


async def fetch_exchange_rate_offloaded(currency: str) -> float:
    def blocking_fetch() -> float:
        with urllib.request.urlopen(EXCHANGE_URL.format(currency=currency), timeout=5) as response:
            return float(json.load(response)["rate"])

    return await asyncio.to_thread(blocking_fetch)


def fetch_rates(currencies: list[str]) -> dict[str, float]:
    rates = {}
    for currency in currencies:
        with urllib.request.urlopen(EXCHANGE_URL.format(currency=currency), timeout=5) as response:
            rates[currency] = float(json.load(response)["rate"])
    return rates
