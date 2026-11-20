import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiosqlite

DB_DIR = Path(os.getenv("REVIEW_DB_DIR", "/app/review/data"))
DB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DB_DIR / "review.db"

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
        CREATE TABLE IF NOT EXISTS reviews (
            id                TEXT PRIMARY KEY,
            book_id           TEXT NOT NULL,
            user_id           TEXT NOT NULL,
            user_name         TEXT NOT NULL,
            rating            INTEGER NOT NULL,
            text              TEXT,
            status            TEXT NOT NULL,
            verified_purchase INTEGER NOT NULL DEFAULT 0,
            created_at        TEXT NOT NULL,
            moderated_at      TEXT,
            UNIQUE(user_id, book_id)
        );
        CREATE INDEX IF NOT EXISTS idx_reviews_book ON reviews(book_id, status);

        CREATE TABLE IF NOT EXISTS verified_purchases (
            user_id  TEXT NOT NULL,
            book_id  TEXT NOT NULL,
            added_at TEXT NOT NULL,
            PRIMARY KEY (user_id, book_id)
        );
        """
    )
    await conn.commit()


def _row_to_review(row) -> dict:
    return {
        "id": row["id"],
        "bookId": row["book_id"],
        "userId": row["user_id"],
        "userName": row["user_name"],
        "rating": row["rating"],
        "text": row["text"],
        "status": row["status"],
        "verifiedPurchase": bool(row["verified_purchase"]),
        "createdAt": row["created_at"],
        "moderatedAt": row["moderated_at"],
    }


async def add_verified_purchase(user_id: str, book_id: str) -> None:
    conn = await get_conn()
    await conn.execute(
        "INSERT OR IGNORE INTO verified_purchases (user_id, book_id, added_at) VALUES (?, ?, ?)",
        (str(user_id), str(book_id), datetime.now(timezone.utc).isoformat()),
    )
    await conn.commit()


async def has_verified_purchase(user_id: str, book_id: str) -> bool:
    conn = await get_conn()
    async with conn.execute(
        "SELECT 1 FROM verified_purchases WHERE user_id = ? AND book_id = ?",
        (str(user_id), str(book_id)),
    ) as cur:
        return await cur.fetchone() is not None


async def get_review(review_id) -> Optional[dict]:
    conn = await get_conn()
    async with conn.execute("SELECT * FROM reviews WHERE id = ?", (str(review_id),)) as cur:
        row = await cur.fetchone()
    return _row_to_review(row) if row else None


async def get_user_review(user_id, book_id) -> Optional[dict]:
    conn = await get_conn()
    async with conn.execute(
        "SELECT * FROM reviews WHERE user_id = ? AND book_id = ?",
        (str(user_id), str(book_id)),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_review(row) if row else None


async def create_review(book_id, user_id, user_name, rating, text) -> dict:
    conn = await get_conn()
    rid = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    verified = await has_verified_purchase(user_id, book_id)
    await conn.execute(
        "INSERT INTO reviews (id, book_id, user_id, user_name, rating, text, status, "
        "verified_purchase, created_at) VALUES (?, ?, ?, ?, ?, ?, 'PENDING', ?, ?)",
        (rid, str(book_id), str(user_id), user_name, rating, text,
         1 if verified else 0, now),
    )
    await conn.commit()
    return await get_review(rid)


async def moderate_review(review_id, approve: bool) -> Optional[dict]:
    conn = await get_conn()
    status = "PUBLISHED" if approve else "REJECTED"
    await conn.execute(
        "UPDATE reviews SET status = ?, moderated_at = ? WHERE id = ?",
        (status, datetime.now(timezone.utc).isoformat(), str(review_id)),
    )
    await conn.commit()
    return await get_review(review_id)


async def list_published_reviews(book_id, page: int, size: int) -> dict:
    conn = await get_conn()
    async with conn.execute(
        "SELECT COUNT(*) FROM reviews WHERE book_id = ? AND status = 'PUBLISHED'",
        (str(book_id),),
    ) as cur:
        total = (await cur.fetchone())[0]
    async with conn.execute(
        "SELECT * FROM reviews WHERE book_id = ? AND status = 'PUBLISHED' "
        "ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (str(book_id), size, page * size),
    ) as cur:
        rows = await cur.fetchall()
    total_pages = (total + size - 1) // size if size else 0
    return {
        "content": [_row_to_review(r) for r in rows],
        "page": page, "size": size,
        "totalElements": total, "totalPages": total_pages,
    }


async def get_rating(book_id) -> dict:
    conn = await get_conn()
    async with conn.execute(
        "SELECT AVG(rating) AS avg, COUNT(*) AS cnt FROM reviews "
        "WHERE book_id = ? AND status = 'PUBLISHED'",
        (str(book_id),),
    ) as cur:
        row = await cur.fetchone()
    avg = round(row["avg"], 2) if row["avg"] is not None else 0.0
    return {"bookId": str(book_id), "averageRating": avg, "totalReviews": row["cnt"]}
