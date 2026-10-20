"""A tiny in-process router and the command-line entry point."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

Handler = Callable[..., Awaitable[Any]]


class Router:
    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], Handler] = {}

    def get(self, path: str) -> Callable[[Handler], Handler]:
        def register(handler: Handler) -> Handler:
            self.routes[("GET", path)] = handler
            return handler

        return register

    async def dispatch(self, method: str, path: str, **params: Any) -> Any:
        handler = self.routes[(method, path)]
        return await handler(**params)


router = Router()


def main(argv: list[str] | None = None) -> int:
    from shopfast import api, catalog, db, orders, reporting

    del api  # imported for its route registrations

    parser = argparse.ArgumentParser(prog="shopfast")
    parser.add_argument("command", choices=["report", "product"])
    parser.add_argument("--orders", type=int, default=20)
    parser.add_argument("--sku", default="SKU-000001")
    args = parser.parse_args(argv)

    products = catalog.build_catalog(seed=7, size=200)
    if args.command == "product":
        product = asyncio.run(router.dispatch("GET", "/products/{sku}", catalog=products, sku=args.sku))
        print(product)
        return 0
    conn = db.connect()
    db.seed(
        conn,
        [(i, f"Customer {i}", "gold" if i % 5 == 0 else "basic") for i in range(1, 11)],
        [(i, 1 + i % 10, 1000 + i * 37) for i in range(1, args.orders + 1)],
    )
    summaries = orders.order_summaries(conn, list(range(1, args.orders + 1)))
    print(reporting.render_sales_report(summaries), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
