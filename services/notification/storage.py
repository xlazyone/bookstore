import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiosqlite

DB_DIR = Path(os.getenv("NOTIFICATION_DB_DIR", "/app/notification/data"))
DB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DB_DIR / "notification.db"

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
        CREATE TABLE IF NOT EXISTS notifications (
            id                  TEXT PRIMARY KEY,
            recipient_email     TEXT NOT NULL,
            type                TEXT NOT NULL,
            status              TEXT NOT NULL,
            subject             TEXT,
            payload             TEXT NOT NULL,
            provider_message_id TEXT,
            error_message       TEXT,
            retry_count         INTEGER NOT NULL DEFAULT 0,
            related_order_id    TEXT,
            created_at          TEXT NOT NULL,
            sent_at             TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_notif_order ON notifications(related_order_id);
        CREATE INDEX IF NOT EXISTS idx_notif_status ON notifications(status);
        """
    )
    await conn.commit()


def _row_to_notification(row) -> dict:
    return {
        "id": row["id"],
        "recipientEmail": row["recipient_email"],
        "type": row["type"],
        "status": row["status"],
        "subject": row["subject"],
        "payload": json.loads(row["payload"]),
        "providerMessageId": row["provider_message_id"],
        "errorMessage": row["error_message"],
        "retryCount": row["retry_count"],
        "createdAt": row["created_at"],
        "sentAt": row["sent_at"],
    }


async def get_notification(notif_id) -> Optional[dict]:
    conn = await get_conn()
    async with conn.execute(
        "SELECT * FROM notifications WHERE id = ?", (str(notif_id),)
    ) as cur:
        row = await cur.fetchone()
    return _row_to_notification(row) if row else None


async def create_notification(recipient_email, type_, subject, payload,
                              related_order_id: Optional[str]) -> dict:
    conn = await get_conn()
    nid = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    await conn.execute(
        "INSERT INTO notifications (id, recipient_email, type, status, subject, payload, "
        "retry_count, related_order_id, created_at) "
        "VALUES (?, ?, ?, 'QUEUED', ?, ?, 0, ?, ?)",
        (nid, recipient_email, type_, subject, json.dumps(payload, default=str),
         related_order_id, now),
    )
    await conn.commit()
    return await get_notification(nid)


async def mark_sent(notif_id, provider_message_id: Optional[str]) -> Optional[dict]:
    conn = await get_conn()
    await conn.execute(
        "UPDATE notifications SET status = 'SENT', sent_at = ?, provider_message_id = ?, "
        "error_message = NULL WHERE id = ?",
        (datetime.now(timezone.utc).isoformat(), provider_message_id, str(notif_id)),
    )
    await conn.commit()
    return await get_notification(notif_id)


async def mark_failed(notif_id, error: str) -> Optional[dict]:
    conn = await get_conn()
    await conn.execute(
        "UPDATE notifications SET status = 'FAILED', error_message = ?, "
        "retry_count = retry_count + 1 WHERE id = ?",
        (error[:500], str(notif_id)),
    )
    await conn.commit()
    return await get_notification(notif_id)


async def list_notifications(order_id: Optional[str], status: Optional[str],
                             page: int, size: int) -> dict:
    conn = await get_conn()
    where, params = [], []
    if order_id:
        where.append("related_order_id = ?"); params.append(str(order_id))
    if status:
        where.append("status = ?"); params.append(status)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    async with conn.execute(
        f"SELECT COUNT(*) FROM notifications {where_sql}", params
    ) as cur:
        total = (await cur.fetchone())[0]
    async with conn.execute(
        f"SELECT * FROM notifications {where_sql} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        params + [size, page * size],
    ) as cur:
        rows = await cur.fetchall()
    total_pages = (total + size - 1) // size if size else 0
    return {
        "content": [_row_to_notification(r) for r in rows],
        "page": page, "size": size,
        "totalElements": total, "totalPages": total_pages,
    }
