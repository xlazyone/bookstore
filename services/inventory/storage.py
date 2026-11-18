import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import aiosqlite

DB_DIR = Path(os.getenv("INVENTORY_DB_DIR", "/app/inventory/data"))
DB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DB_DIR / "inventory.db"

RESERVATION_TTL_MINUTES = int(os.getenv("RESERVATION_TTL_MINUTES", "15"))

# book_id из catalog seed — см. шаг 0
SEED_BOOK_IDS = [
    "9a4d1e2c-6b7f-4a08-b3c5-0e1f2d3c4b5a",  # Мастер и Маргарита
    "aaaa0001-0001-0001-0001-000000000001",  # Война и мир
    "aaaa0002-0002-0002-0002-000000000002",  # Солярис
]

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
        CREATE TABLE IF NOT EXISTS stock_items (
            book_id    TEXT PRIMARY KEY,
            available  INTEGER NOT NULL DEFAULT 0,
            reserved   INTEGER NOT NULL DEFAULT 0,
            total      INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS reservations (
            reservation_id TEXT PRIMARY KEY,
            order_id       TEXT NOT NULL UNIQUE,
            status         TEXT NOT NULL,
            items          TEXT NOT NULL,
            expires_at     TEXT NOT NULL,
            created_at     TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_reservations_status ON reservations(status);
        CREATE INDEX IF NOT EXISTS idx_reservations_expires ON reservations(expires_at);
        """
    )
    await conn.commit()


async def seed_if_empty() -> None:
    conn = await get_conn()
    async with conn.execute("SELECT COUNT(*) FROM stock_items") as cur:
        if (await cur.fetchone())[0] > 0:
            return
    now = datetime.now(timezone.utc).isoformat()
    for book_id in SEED_BOOK_IDS:
        await conn.execute(
            "INSERT OR IGNORE INTO stock_items (book_id, available, reserved, total, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (book_id, 10, 0, 10, now),
        )
    await conn.commit()


def _row_to_stock(row) -> dict:
    return {
        "bookId": row["book_id"],
        "available": row["available"],
        "reserved": row["reserved"],
        "total": row["total"],
        "updatedAt": row["updated_at"],
    }


def _row_to_reservation(row) -> dict:
    return {
        "reservationId": row["reservation_id"],
        "orderId": row["order_id"],
        "status": row["status"],
        "items": json.loads(row["items"]),
        "expiresAt": row["expires_at"],
    }


async def get_stock(book_id) -> Optional[dict]:
    conn = await get_conn()
    async with conn.execute("SELECT * FROM stock_items WHERE book_id = ?", (str(book_id),)) as cur:
        row = await cur.fetchone()
    return _row_to_stock(row) if row else None


async def list_stock(low_stock: bool, page: int, size: int) -> dict:
    conn = await get_conn()
    where = "WHERE available <= 3" if low_stock else ""
    async with conn.execute(f"SELECT COUNT(*) FROM stock_items {where}") as cur:
        total = (await cur.fetchone())[0]
    async with conn.execute(
        f"SELECT * FROM stock_items {where} ORDER BY book_id LIMIT ? OFFSET ?",
        (size, page * size),
    ) as cur:
        rows = await cur.fetchall()
    total_pages = (total + size - 1) // size if size else 0
    return {
        "content": [_row_to_stock(r) for r in rows],
        "page": page, "size": size,
        "totalElements": total, "totalPages": total_pages,
    }


async def update_stock(book_id, delta: int) -> dict:
    conn = await get_conn()
    async with conn.execute("SELECT * FROM stock_items WHERE book_id = ?", (str(book_id),)) as cur:
        row = await cur.fetchone()
    if not row:
        raise ValueError("not_found")
    new_available = row["available"] + delta
    new_total = row["total"] + delta
    if new_available < 0 or new_total < 0:
        raise ValueError("negative")
    await conn.execute(
        "UPDATE stock_items SET available = ?, total = ?, updated_at = ? WHERE book_id = ?",
        (new_available, new_total, datetime.now(timezone.utc).isoformat(), str(book_id)),
    )
    await conn.commit()
    return await get_stock(book_id)


async def get_reservation_by_order(order_id) -> Optional[dict]:
    conn = await get_conn()
    async with conn.execute("SELECT * FROM reservations WHERE order_id = ?", (str(order_id),)) as cur:
        row = await cur.fetchone()
    return _row_to_reservation(row) if row else None


async def create_reservation(order_id, items: list[dict]) -> dict:
    """Атомарно резервирует. При нехватке остатка бросает ValueError('insufficient')."""
    conn = await get_conn()
    now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(minutes=RESERVATION_TTL_MINUTES)).isoformat()

    await conn.execute("BEGIN")
    try:
        for item in items:
            async with conn.execute(
                "SELECT available FROM stock_items WHERE book_id = ?", (str(item["bookId"]),)
            ) as cur:
                row = await cur.fetchone()
            if not row:
                await conn.execute("ROLLBACK")
                raise ValueError("book_not_found")
            if row["available"] < item["quantity"]:
                await conn.execute("ROLLBACK")
                raise ValueError("insufficient")

        for item in items:
            await conn.execute(
                "UPDATE stock_items SET available = available - ?, reserved = reserved + ?, "
                "updated_at = ? WHERE book_id = ?",
                (item["quantity"], item["quantity"], now.isoformat(), str(item["bookId"])),
            )

        reservation_id = str(uuid.uuid4())
        await conn.execute(
            "INSERT INTO reservations (reservation_id, order_id, status, items, expires_at, created_at) "
            "VALUES (?, ?, 'ACTIVE', ?, ?, ?)",
            (reservation_id, str(order_id), json.dumps([
                {"bookId": str(i["bookId"]), "quantity": i["quantity"]} for i in items
            ]), expires_at, now.isoformat()),
        )
        await conn.execute("COMMIT")
    except Exception:
        try:
            await conn.execute("ROLLBACK")
        except Exception:
            pass
        raise

    return await get_reservation_by_order(order_id)


async def release_reservation(order_id, new_status: str = "RELEASED") -> Optional[dict]:
    """Возвращает зарезервированное обратно в available. Идемпотентно."""
    conn = await get_conn()
    async with conn.execute("SELECT * FROM reservations WHERE order_id = ?", (str(order_id),)) as cur:
        row = await cur.fetchone()
    if not row:
        return None
    if row["status"] != "ACTIVE":
        return _row_to_reservation(row)

    items = json.loads(row["items"])
    now = datetime.now(timezone.utc).isoformat()
    await conn.execute("BEGIN")
    try:
        for item in items:
            await conn.execute(
                "UPDATE stock_items SET available = available + ?, reserved = reserved - ?, "
                "updated_at = ? WHERE book_id = ?",
                (item["quantity"], item["quantity"], now, item["bookId"]),
            )
        await conn.execute(
            "UPDATE reservations SET status = ? WHERE order_id = ?", (new_status, str(order_id))
        )
        await conn.execute("COMMIT")
    except Exception:
        await conn.execute("ROLLBACK")
        raise
    return await get_reservation_by_order(order_id)


async def confirm_reservation(order_id) -> Optional[dict]:
    """При подтверждении заказа резерв становится окончательным списанием: total уменьшается."""
    conn = await get_conn()
    async with conn.execute("SELECT * FROM reservations WHERE order_id = ?", (str(order_id),)) as cur:
        row = await cur.fetchone()
    if not row:
        return None
    if row["status"] != "ACTIVE":
        return _row_to_reservation(row)

    items = json.loads(row["items"])
    now = datetime.now(timezone.utc).isoformat()
    await conn.execute("BEGIN")
    try:
        for item in items:
            await conn.execute(
                "UPDATE stock_items SET reserved = reserved - ?, total = total - ?, "
                "updated_at = ? WHERE book_id = ?",
                (item["quantity"], item["quantity"], now, item["bookId"]),
            )
        await conn.execute(
            "UPDATE reservations SET status = 'CONFIRMED' WHERE order_id = ?", (str(order_id),)
        )
        await conn.execute("COMMIT")
    except Exception:
        await conn.execute("ROLLBACK")
        raise
    return await get_reservation_by_order(order_id)


async def expire_overdue() -> list[dict]:
    """Возвращает список просроченных ACTIVE-резервов, помечая их EXPIRED."""
    conn = await get_conn()
    now = datetime.now(timezone.utc).isoformat()
    async with conn.execute(
        "SELECT * FROM reservations WHERE status = 'ACTIVE' AND expires_at < ?", (now,)
    ) as cur:
        rows = await cur.fetchall()
    expired = []
    for row in rows:
        released = await release_reservation(row["order_id"], new_status="EXPIRED")
        if released:
            expired.append(released)
    return expired
