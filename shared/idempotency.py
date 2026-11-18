"""Хранилище Idempotency-Key в SQLite (aiosqlite)."""
import json
from typing import Optional

import aiosqlite


async def init_idempotency_table(conn: aiosqlite.Connection) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS idempotency_keys (
            key         TEXT PRIMARY KEY,
            response    TEXT NOT NULL,
            status_code INTEGER NOT NULL,
            created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    await conn.commit()


async def get_idempotent_response(
    conn: aiosqlite.Connection, key: str
) -> Optional[tuple[int, dict]]:
    async with conn.execute(
        "SELECT status_code, response FROM idempotency_keys WHERE key = ?", (key,)
    ) as cur:
        row = await cur.fetchone()
    if row:
        return row[0], json.loads(row[1])
    return None


async def store_idempotent_response(
    conn: aiosqlite.Connection, key: str, status_code: int, response: dict
) -> None:
    await conn.execute(
        "INSERT OR REPLACE INTO idempotency_keys (key, response, status_code) VALUES (?, ?, ?)",
        (key, json.dumps(response, default=str), status_code),
    )
    await conn.commit()
