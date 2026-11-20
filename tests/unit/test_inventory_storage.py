import importlib.util
import os
import uuid
from pathlib import Path

import pytest


def _load(tmp_path: Path):
    os.environ["INVENTORY_DB_DIR"] = str(tmp_path)
    path = Path(__file__).resolve().parents[2] / "services" / "inventory" / "storage.py"
    spec = importlib.util.spec_from_file_location("inv_storage", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BOOK = "9a4d1e2c-6b7f-4a08-b3c5-0e1f2d3c4b5a"


@pytest.fixture
async def inv(tmp_path):
    mod = _load(tmp_path)
    await mod.init_db()
    await mod.seed_if_empty()
    yield mod
    await mod.close_db()


async def test_reserve_success(inv):
    order_id = str(uuid.uuid4())
    res = await inv.create_reservation(order_id, [{"bookId": BOOK, "quantity": 2}])
    assert res["status"] == "ACTIVE"
    stock = await inv.get_stock(BOOK)
    assert stock["available"] == 8 and stock["reserved"] == 2


async def test_reserve_insufficient(inv):
    with pytest.raises(ValueError, match="insufficient"):
        await inv.create_reservation(str(uuid.uuid4()), [{"bookId": BOOK, "quantity": 99}])


async def test_reserve_unknown_book(inv):
    with pytest.raises(ValueError, match="book_not_found"):
        await inv.create_reservation(str(uuid.uuid4()),
                                     [{"bookId": str(uuid.uuid4()), "quantity": 1}])


async def test_reserve_idempotent_by_order(inv):
    order_id = str(uuid.uuid4())
    a = await inv.create_reservation(order_id, [{"bookId": BOOK, "quantity": 1}])
    # повтор того же orderId через storage не должен создать второй резерв
    # (в HTTP-слое это делает get_idempotent_response; здесь проверяем уникальность orderId)
    existing = await inv.get_reservation_by_order(order_id)
    assert existing["reservationId"] == a["reservationId"]


async def test_release_restores_available(inv):
    order_id = str(uuid.uuid4())
    await inv.create_reservation(order_id, [{"bookId": BOOK, "quantity": 3}])
    released = await inv.release_reservation(order_id, new_status="RELEASED")
    assert released["status"] == "RELEASED"
    stock = await inv.get_stock(BOOK)
    assert stock["available"] == 10 and stock["reserved"] == 0


async def test_confirm_decrements_total(inv):
    order_id = str(uuid.uuid4())
    await inv.create_reservation(order_id, [{"bookId": BOOK, "quantity": 2}])
    confirmed = await inv.confirm_reservation(order_id)
    assert confirmed["status"] == "CONFIRMED"
    stock = await inv.get_stock(BOOK)
    assert stock["available"] == 8 and stock["reserved"] == 0 and stock["total"] == 8


async def test_expire_overdue(inv):
    # TTL 15 мин — принудительно сдвинем expires_at в прошлое через SQL
    order_id = str(uuid.uuid4())
    await inv.create_reservation(order_id, [{"bookId": BOOK, "quantity": 1}])
    conn = await inv.get_conn()
    await conn.execute("UPDATE reservations SET expires_at = '2000-01-01T00:00:00' WHERE order_id = ?",
                       (order_id,))
    await conn.commit()
    expired = await inv.expire_overdue()
    assert any(e["orderId"] == order_id for e in expired)
    stock = await inv.get_stock(BOOK)
    assert stock["available"] == 10
