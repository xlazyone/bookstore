import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import aiosqlite

DB_DIR = Path(os.getenv("ORDER_DB_DIR", "/app/order/data"))
DB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DB_DIR / "order.db"

RESERVATION_TTL_MINUTES = int(os.getenv("RESERVATION_TTL_MINUTES", "15"))

_conn: Optional[aiosqlite.Connection] = None


async def get_conn() -> aiosqlite.Connection:
    global _conn
    if _conn is None:
        _conn = await aiosqlite.connect(DB_PATH, isolation_level=None)
        _conn.row_factory = aiosqlite.Row
        await _conn.execute("PRAGMA journal_mode=WAL")
    return _conn


async def close_db() -> None:
    global _conn
    if _conn:
        await _conn.close()
        _conn = None


async def init_db() -> None:
    conn = await get_conn()
    await conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS cart_items (
            item_id   TEXT PRIMARY KEY,
            user_id   TEXT NOT NULL,
            book_id   TEXT NOT NULL,
            title     TEXT NOT NULL,
            price     REAL NOT NULL,
            currency  TEXT NOT NULL DEFAULT 'RUB',
            quantity  INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(user_id, book_id)
        );
        CREATE INDEX IF NOT EXISTS idx_cart_user ON cart_items(user_id);

        CREATE TABLE IF NOT EXISTS orders (
            id           TEXT PRIMARY KEY,
            user_id      TEXT NOT NULL,
            status       TEXT NOT NULL,
            total_amount REAL NOT NULL,
            currency     TEXT NOT NULL,
            payment_url  TEXT,
            expires_at   TEXT,
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id);

        CREATE TABLE IF NOT EXISTS order_items (
            order_id  TEXT NOT NULL,
            book_id   TEXT NOT NULL,
            title     TEXT NOT NULL,
            price     REAL NOT NULL,
            quantity  INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items(order_id);
        """
    )
    await conn.commit()


# ---------- Cart ----------

async def get_cart(user_id) -> dict:
    conn = await get_conn()
    async with conn.execute(
        "SELECT * FROM cart_items WHERE user_id = ? ORDER BY created_at",
        (str(user_id),),
    ) as cur:
        rows = await cur.fetchall()
    items = [
        {"itemId": r["item_id"], "bookId": r["book_id"], "title": r["title"],
         "price": r["price"], "quantity": r["quantity"]}
        for r in rows
    ]
    total = sum(i["price"] * i["quantity"] for i in items)
    currency = rows[0]["currency"] if rows else "RUB"
    return {"userId": str(user_id), "items": items, "totalAmount": total, "currency": currency}


async def add_cart_item(user_id, book: dict, quantity: int) -> dict:
    conn = await get_conn()
    now = datetime.now(timezone.utc).isoformat()
    async with conn.execute(
        "SELECT * FROM cart_items WHERE user_id = ? AND book_id = ?",
        (str(user_id), str(book["id"])),
    ) as cur:
        row = await cur.fetchone()
    if row:
        await conn.execute(
            "UPDATE cart_items SET quantity = quantity + ? WHERE item_id = ?",
            (quantity, row["item_id"]),
        )
    else:
        await conn.execute(
            "INSERT INTO cart_items (item_id, user_id, book_id, title, price, currency, quantity, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), str(user_id), str(book["id"]),
             book["title"], book["price"], book.get("currency", "RUB"), quantity, now),
        )
    await conn.commit()
    return await get_cart(user_id)


async def remove_cart_item(user_id, item_id) -> bool:
    conn = await get_conn()
    cur = await conn.execute(
        "DELETE FROM cart_items WHERE item_id = ? AND user_id = ?",
        (str(item_id), str(user_id)),
    )
    await conn.commit()
    return cur.rowcount > 0


async def clear_cart(user_id) -> None:
    conn = await get_conn()
    await conn.execute("DELETE FROM cart_items WHERE user_id = ?", (str(user_id),))
    await conn.commit()


# ---------- Orders ----------

def _row_to_order(row, items: list[dict]) -> dict:
    return {
        "id": row["id"],
        "userId": row["user_id"],
        "status": row["status"],
        "items": items,
        "totalAmount": row["total_amount"],
        "currency": row["currency"],
        "paymentUrl": row["payment_url"],
        "expiresAt": row["expires_at"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


async def _load_items(order_id) -> list[dict]:
    conn = await get_conn()
    async with conn.execute(
        "SELECT * FROM order_items WHERE order_id = ?", (str(order_id),)
    ) as cur:
        rows = await cur.fetchall()
    return [
        {"bookId": r["book_id"], "title": r["title"],
         "price": r["price"], "quantity": r["quantity"]}
        for r in rows
    ]


async def get_order(order_id) -> Optional[dict]:
    conn = await get_conn()
    async with conn.execute("SELECT * FROM orders WHERE id = ?", (str(order_id),)) as cur:
        row = await cur.fetchone()
    if not row:
        return None
    return _row_to_order(row, await _load_items(order_id))


async def get_order_by_status_user(user_id, order_id) -> Optional[dict]:
    order = await get_order(order_id)
    if not order or order["userId"] != str(user_id):
        return None
    return order


async def list_orders(user_id, page: int, size: int) -> dict:
    conn = await get_conn()
    async with conn.execute(
        "SELECT COUNT(*) FROM orders WHERE user_id = ?", (str(user_id),)
    ) as cur:
        total = (await cur.fetchone())[0]
    async with conn.execute(
        "SELECT * FROM orders WHERE user_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (str(user_id), size, page * size),
    ) as cur:
        rows = await cur.fetchall()
    content = [
        {"id": r["id"], "status": r["status"], "totalAmount": r["total_amount"],
         "currency": r["currency"], "createdAt": r["created_at"]}
        for r in rows
    ]
    total_pages = (total + size - 1) // size if size else 0
    return {
        "content": content, "page": page, "size": size,
        "totalElements": total, "totalPages": total_pages,
    }


async def create_order(user_id, items: list[dict], currency: str) -> dict:
    conn = await get_conn()
    now = datetime.now(timezone.utc)
    order_id = str(uuid.uuid4())
    total = sum(i["price"] * i["quantity"] for i in items)
    expires_at = (now + timedelta(minutes=RESERVATION_TTL_MINUTES)).isoformat()

    await conn.execute("BEGIN")
    try:
        await conn.execute(
            "INSERT INTO orders (id, user_id, status, total_amount, currency, "
            "payment_url, expires_at, created_at, updated_at) "
            "VALUES (?, ?, 'PENDING_PAYMENT', ?, ?, NULL, ?, ?, ?)",
            (order_id, str(user_id), total, currency, expires_at,
             now.isoformat(), now.isoformat()),
        )
        for i in items:
            await conn.execute(
                "INSERT INTO order_items (order_id, book_id, title, price, quantity) "
                "VALUES (?, ?, ?, ?, ?)",
                (order_id, str(i["bookId"]), i["title"], i["price"], i["quantity"]),
            )
        await conn.execute("COMMIT")
    except Exception:
        await conn.execute("ROLLBACK")
        raise
    return await get_order(order_id)


async def update_order_status(order_id, status: str) -> Optional[dict]:
    conn = await get_conn()
    await conn.execute(
        "UPDATE orders SET status = ?, updated_at = ? WHERE id = ?",
        (status, datetime.now(timezone.utc).isoformat(), str(order_id)),
    )
    await conn.commit()
    return await get_order(order_id)


async def set_payment_url(order_id, payment_url: str) -> None:
    conn = await get_conn()
    await conn.execute(
        "UPDATE orders SET payment_url = ?, updated_at = ? WHERE id = ?",
        (payment_url, datetime.now(timezone.utc).isoformat(), str(order_id)),
    )
    await conn.commit()
