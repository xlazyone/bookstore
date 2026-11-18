import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiosqlite

DB_DIR = Path(os.getenv("CATALOG_DB_DIR", "/app/catalog/data"))
DB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DB_DIR / "catalog.db"

_conn: Optional[aiosqlite.Connection] = None


async def get_conn() -> aiosqlite.Connection:
    global _conn
    if _conn is None:
        _conn = await aiosqlite.connect(DB_PATH)
        _conn.row_factory = aiosqlite.Row
        await _conn.execute("PRAGMA journal_mode=WAL")
        await _conn.execute("PRAGMA foreign_keys=ON")
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
        CREATE TABLE IF NOT EXISTS authors (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            bio TEXT
        );
        CREATE TABLE IF NOT EXISTS genres (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS books (
            id TEXT PRIMARY KEY,
            isbn TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            title_lower TEXT NOT NULL DEFAULT '',
            description TEXT,
            author_id TEXT NOT NULL,
            author_name_lower TEXT NOT NULL DEFAULT '',
            genre_id TEXT NOT NULL,
            price REAL NOT NULL,
            currency TEXT NOT NULL DEFAULT 'RUB',
            cover_url TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_books_active ON books(active);
        CREATE INDEX IF NOT EXISTS idx_books_genre ON books(genre_id);
        CREATE INDEX IF NOT EXISTS idx_books_author ON books(author_id);
        CREATE INDEX IF NOT EXISTS idx_books_title_lower ON books(title_lower);
	CREATE INDEX IF NOT EXISTS idx_books_author_lower ON books(author_name_lower);
        """
    )
    await conn.commit()


async def seed_if_empty() -> None:
    conn = await get_conn()
    async with conn.execute("SELECT COUNT(*) FROM books") as cur:
        row = await cur.fetchone()
    if row[0] > 0:
        return

    now = datetime.now(timezone.utc).isoformat()
    await conn.executescript(
        """
        INSERT OR IGNORE INTO authors (id, name, bio) VALUES
            ('11111111-1111-1111-1111-111111111111', 'Михаил Булгаков', 'Русский писатель и драматург'),
            ('22222222-2222-2222-2222-222222222222', 'Лев Толстой', 'Русский писатель и мыслитель');
        INSERT OR IGNORE INTO genres (id, name) VALUES
            ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'Роман'),
            ('bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', 'Фантастика');
        """
    )
    for isbn, title, desc, author_id, genre_id, price in [
        ("9783161484100", "Мастер и Маргарита", "Роман о добре и зле",
         "11111111-1111-1111-1111-111111111111", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", 900.0),
        ("9785000000001", "Война и мир", "Роман-эпопея",
         "22222222-2222-2222-2222-222222222222", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", 1200.0),
        ("9785000000002", "Солярис", "Философская фантастика",
         "22222222-2222-2222-2222-222222222222", "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", 700.0),
    ]:
        # получаем имя автора для нормализации
        async with conn.execute("SELECT name FROM authors WHERE id = ?", (author_id,)) as cur:
            author_row = await cur.fetchone()
        author_name = author_row["name"] if author_row else ""

        await conn.execute(
            "INSERT INTO books (id, isbn, title, title_lower, description, author_id, "
            "author_name_lower, genre_id, price, currency, cover_url, active, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), isbn, title, title.lower(), desc, author_id,
             author_name.lower(), genre_id, price,
             "RUB", f"http://minio.local/covers/{isbn}.jpg", 1, now, now),
        )
    await conn.commit()


async def _row_to_book(row) -> dict:
    conn = await get_conn()
    async with conn.execute("SELECT * FROM authors WHERE id = ?", (row["author_id"],)) as cur:
        author_row = await cur.fetchone()
    async with conn.execute("SELECT * FROM genres WHERE id = ?", (row["genre_id"],)) as cur:
        genre_row = await cur.fetchone()
    return {
        "id": row["id"],
        "isbn": row["isbn"],
        "title": row["title"],
        "description": row["description"],
        "author": {"id": author_row["id"], "name": author_row["name"], "bio": author_row["bio"]},
        "genre": {"id": genre_row["id"], "name": genre_row["name"]},
        "price": row["price"],
        "currency": row["currency"],
        "coverUrl": row["cover_url"],
        "active": bool(row["active"]),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


async def list_books(q, genre_id, author_id, min_price, max_price, page, size) -> dict:
    conn = await get_conn()
    where = ["b.active = 1"]
    params: list = []
    if q:
        where.append("(b.title_lower LIKE ? OR b.author_name_lower LIKE ?)")
        like = f"%{q.lower()}%"
        params.extend([like, like])
    if genre_id:
        where.append("b.genre_id = ?")
        params.append(str(genre_id))
    if author_id:
        where.append("b.author_id = ?")
        params.append(str(author_id))
    if min_price is not None:
        where.append("b.price >= ?")
        params.append(min_price)
    if max_price is not None:
        where.append("b.price <= ?")
        params.append(max_price)
    where_sql = " AND ".join(where)
    base = f"FROM books b JOIN authors a ON b.author_id = a.id WHERE {where_sql}"

    async with conn.execute(f"SELECT COUNT(*) {base}", params) as cur:
        total = (await cur.fetchone())[0]

    async with conn.execute(
        f"SELECT b.* {base} ORDER BY b.created_at DESC LIMIT ? OFFSET ?",
        params + [size, page * size],
    ) as cur:
        rows = await cur.fetchall()

    content = []
    for row in rows:
        book = await _row_to_book(row)
        content.append({
            "id": book["id"],
            "title": book["title"],
            "author": book["author"],
            "price": book["price"],
            "currency": book["currency"],
            "coverUrl": book["coverUrl"],
        })

    total_pages = (total + size - 1) // size if size > 0 else 0
    return {
        "content": content,
        "page": page,
        "size": size,
        "totalElements": total,
        "totalPages": total_pages,
    }


async def get_book(book_id) -> Optional[dict]:
    conn = await get_conn()
    async with conn.execute("SELECT * FROM books WHERE id = ?", (str(book_id),)) as cur:
        row = await cur.fetchone()
    return await _row_to_book(row) if row else None


async def get_books_by_ids(ids: list) -> list[dict]:
    if not ids:
        return []
    conn = await get_conn()
    placeholders = ",".join("?" * len(ids))
    async with conn.execute(
        f"SELECT * FROM books WHERE id IN ({placeholders})",
        [str(i) for i in ids],
    ) as cur:
        rows = await cur.fetchall()
    return [await _row_to_book(r) for r in rows]


async def get_author(author_id) -> Optional[dict]:
    conn = await get_conn()
    async with conn.execute("SELECT * FROM authors WHERE id = ?", (str(author_id),)) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def get_genre(genre_id) -> Optional[dict]:
    conn = await get_conn()
    async with conn.execute("SELECT * FROM genres WHERE id = ?", (str(genre_id),)) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def list_genres() -> list[dict]:
    conn = await get_conn()
    async with conn.execute("SELECT * FROM genres ORDER BY name") as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def isbn_exists(isbn: str) -> bool:
    conn = await get_conn()
    async with conn.execute("SELECT 1 FROM books WHERE isbn = ?", (isbn,)) as cur:
        return await cur.fetchone() is not None


async def create_book(body) -> dict:
    conn = await get_conn()
    now = datetime.now(timezone.utc).isoformat()
    book_id = str(uuid.uuid4())
    cover_url = f"http://minio.local/covers/{body.isbn}.jpg"

    # получаем имя автора для нормализации
    async with conn.execute("SELECT name FROM authors WHERE id = ?", (str(body.authorId),)) as cur:
        author_row = await cur.fetchone()
    author_name = author_row["name"] if author_row else ""

    await conn.execute(
        "INSERT INTO books (id, isbn, title, title_lower, description, author_id, "
        "author_name_lower, genre_id, price, currency, cover_url, active, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (book_id, body.isbn, body.title, body.title.lower(), body.description,
         str(body.authorId), author_name.lower(), str(body.genreId),
         body.price, body.currency, cover_url, 1, now, now),
    )
    await conn.commit()
    return await get_book(book_id)


async def update_book(book_id, body) -> dict:
    conn = await get_conn()
    fields, params = [], []
    if body.title is not None:
        fields.append("title = ?"); params.append(body.title)
    if body.description is not None:
        fields.append("description = ?"); params.append(body.description)
    if body.genreId is not None:
        fields.append("genre_id = ?"); params.append(str(body.genreId))
    if body.price is not None:
        fields.append("price = ?"); params.append(body.price)
    if body.active is not None:
        fields.append("active = ?"); params.append(1 if body.active else 0)
    if fields:
        fields.append("updated_at = ?"); params.append(datetime.now(timezone.utc).isoformat())
        params.append(str(book_id))
        await conn.execute(f"UPDATE books SET {', '.join(fields)} WHERE id = ?", params)
        await conn.commit()
    return await get_book(book_id)


async def discontinue_book(book_id) -> None:
    conn = await get_conn()
    await conn.execute(
        "UPDATE books SET active = 0, updated_at = ? WHERE id = ?",
        (datetime.now(timezone.utc).isoformat(), str(book_id)),
    )
    await conn.commit()
