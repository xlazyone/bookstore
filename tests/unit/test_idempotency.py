import aiosqlite
import pytest

from shared.idempotency import (
    get_idempotent_response, init_idempotency_table, store_idempotent_response,
)


@pytest.fixture
async def conn():
    c = await aiosqlite.connect(":memory:")
    c.row_factory = aiosqlite.Row
    await init_idempotency_table(c)
    yield c
    await c.close()


async def test_store_and_get(conn):
    await store_idempotent_response(conn, "key-1", 201, {"id": "abc"})
    got = await get_idempotent_response(conn, "key-1")
    assert got == (201, {"id": "abc"})


async def test_missing_returns_none(conn):
    assert await get_idempotent_response(conn, "no-such-key") is None


async def test_overwrite(conn):
    await store_idempotent_response(conn, "k", 200, {"v": 1})
    await store_idempotent_response(conn, "k", 200, {"v": 2})
    status, body = await get_idempotent_response(conn, "k")
    assert status == 200 and body == {"v": 2}
