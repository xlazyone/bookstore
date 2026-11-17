"""Хранилище Idempotency-Key и уже отданных ответов в SQLite."""
import json
import sqlite3
from typing import Optional


def init_idempotency_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS idempotency_keys (
            key         TEXT PRIMARY KEY,
            response    TEXT NOT NULL,
            status_code INTEGER NOT NULL,
            created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()


def get_idempotent_response(
    conn: sqlite3.Connection, key: str
) -> Optional[tuple[int, dict]]:
    row = conn.execute(
        "SELECT status_code, response FROM idempotency_keys WHERE key = ?", (key,)
    ).fetchone()
    if row:
        return row[0], json.loads(row[1])
    return None


def store_idempotent_response(
    conn: sqlite3.Connection, key: str, status_code: int, response: dict
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO idempotency_keys (key, response, status_code) VALUES (?, ?, ?)",
        (key, json.dumps(response), status_code),
    )
    conn.commit()
