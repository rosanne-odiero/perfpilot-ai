from __future__ import annotations

import pytest

from shopfast import catalog, db


@pytest.fixture
def products() -> list[catalog.Product]:
    return catalog.build_catalog(seed=42, size=60)


@pytest.fixture
def conn():
    connection = db.connect()
    db.seed(
        connection,
        [(1, "Ada", "gold"), (2, "Grace", "basic")],
        [(10, 1, 1500), (11, 2, 990), (12, 1, 4000)],
    )
    yield connection
    connection.close()
