import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiosqlite

DB_DIR = Path(os.getenv("PAYMENT_DB_DIR", "/app/payment/data"))
DB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DB_DIR / "payment.db"

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
        CREATE TABLE IF NOT EXISTS payments (
            id          TEXT PRIMARY KEY,
            order_id    TEXT NOT NULL,
            amount      REAL NOT NULL,
            currency    TEXT NOT NULL,
            status      TEXT NOT NULL,
            payment_url TEXT,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_payments_order ON payments(order_id);
        CREATE TABLE IF NOT EXISTS refunds (
            id          TEXT PRIMARY KEY,
            payment_id  TEXT NOT NULL,
            amount      REAL NOT NULL,
            status      TEXT NOT NULL,
            reason      TEXT,
            created_at  TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS processed_webhooks (
            event_id   TEXT PRIMARY KEY,
            processed_at TEXT NOT NULL
        );
        """
    )
    await conn.commit()


def _row_to_payment(row) -> dict:
    return {
        "id": row["id"], "orderId": row["order_id"], "amount": row["amount"],
        "currency": row["currency"], "status": row["status"],
        "paymentUrl": row["payment_url"],
        "createdAt": row["created_at"], "updatedAt": row["updated_at"],
    }


def _row_to_refund(row) -> dict:
    return {
        "id": row["id"], "paymentId": row["payment_id"], "amount": row["amount"],
        "status": row["status"], "reason": row["reason"], "createdAt": row["created_at"],
    }


async def get_payment(payment_id) -> Optional[dict]:
    conn = await get_conn()
    async with conn.execute("SELECT * FROM payments WHERE id = ?", (str(payment_id),)) as cur:
        row = await cur.fetchone()
    return _row_to_payment(row) if row else None


async def get_payment_by_order(order_id) -> Optional[dict]:
    conn = await get_conn()
    async with conn.execute(
        "SELECT * FROM payments WHERE order_id = ? ORDER BY created_at DESC LIMIT 1",
        (str(order_id),),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_payment(row) if row else None


async def insert_payment(payment_id, order_id, amount, currency, payment_url) -> dict:
    conn = await get_conn()
    now = datetime.now(timezone.utc).isoformat()
    await conn.execute(
        "INSERT INTO payments (id, order_id, amount, currency, status, payment_url, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'INITIATED', ?, ?, ?)",
        (str(payment_id), str(order_id), amount, currency, payment_url, now, now),
    )
    await conn.commit()
    return await get_payment(payment_id)


async def update_payment_status(payment_id, status: str) -> Optional[dict]:
    conn = await get_conn()
    await conn.execute(
        "UPDATE payments SET status = ?, updated_at = ? WHERE id = ?",
        (status, datetime.now(timezone.utc).isoformat(), str(payment_id)),
    )
    await conn.commit()
    return await get_payment(payment_id)


async def is_webhook_processed(event_id) -> bool:
    conn = await get_conn()
    async with conn.execute(
        "SELECT 1 FROM processed_webhooks WHERE event_id = ?", (str(event_id),)
    ) as cur:
        return await cur.fetchone() is not None


async def mark_webhook_processed(event_id) -> None:
    conn = await get_conn()
    await conn.execute(
        "INSERT OR IGNORE INTO processed_webhooks (event_id, processed_at) VALUES (?, ?)",
        (str(event_id), datetime.now(timezone.utc).isoformat()),
    )
    await conn.commit()


async def insert_refund(payment_id, amount, reason) -> dict:
    conn = await get_conn()
    now = datetime.now(timezone.utc).isoformat()
    rid = str(uuid.uuid4())
    await conn.execute(
        "INSERT INTO refunds (id, payment_id, amount, status, reason, created_at) "
        "VALUES (?, ?, ?, 'COMPLETED', ?, ?)",
        (rid, str(payment_id), amount, reason, now),
    )
    await conn.commit()
    async with conn.execute("SELECT * FROM refunds WHERE id = ?", (rid,)) as cur:
        row = await cur.fetchone()
    return _row_to_refund(row)


async def get_refund_by_payment(payment_id) -> Optional[dict]:
    conn = await get_conn()
    async with conn.execute(
        "SELECT * FROM refunds WHERE payment_id = ?", (str(payment_id),)
    ) as cur:
        row = await cur.fetchone()
    return _row_to_refund(row) if row else None
