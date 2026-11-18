import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, Query, Request

from shared.auth import require_role
from shared.broker import Broker, DEFAULT_URL
from shared.correlation import CorrelationIdMiddleware, get_correlation_id
from shared.problem import ProblemException, install_problem_handler

import storage
from models import (
    Author, Book, CreateBookRequest, Genre,
    PaginatedBooks, UpdateBookRequest,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("catalog-service")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await storage.init_db()
    await storage.seed_if_empty()
    broker = Broker(url=os.getenv("RABBITMQ_URL", DEFAULT_URL))
    await broker.connect()
    app.state.broker = broker
    logger.info("catalog-service started")
    yield
    await broker.close()
    await storage.close_db()
    logger.info("catalog-service stopped")


app = FastAPI(title="catalog-service", version="1.0.0", lifespan=lifespan)
app.add_middleware(CorrelationIdMiddleware)
install_problem_handler(app)


# ---------- Public ----------

@app.get("/api/v1/books", response_model=PaginatedBooks)
async def list_books(
    q: Optional[str] = Query(None, max_length=200),
    genreId: Optional[uuid.UUID] = None,
    authorId: Optional[uuid.UUID] = None,
    minPrice: Optional[float] = Query(None, ge=0),
    maxPrice: Optional[float] = Query(None, ge=0),
    page: int = Query(0, ge=0),
    size: int = Query(20, ge=1, le=100),
):
    return await storage.list_books(q, genreId, authorId, minPrice, maxPrice, page, size)


@app.get("/api/v1/books/{book_id}", response_model=Book)
async def get_book(book_id: uuid.UUID):
    book = await storage.get_book(book_id)
    if not book:
        raise ProblemException(
            404, "Book not found", f"Book {book_id} not found",
            type_="https://bookstore.local/errors/not-found",
        )
    return book


@app.get("/api/v1/genres", response_model=list[Genre])
async def list_genres():
    return await storage.list_genres()


@app.get("/api/v1/authors/{author_id}", response_model=Author)
async def get_author(author_id: uuid.UUID):
    author = await storage.get_author(author_id)
    if not author:
        raise ProblemException(404, "Author not found", f"Author {author_id} not found")
    return author


# ---------- Admin ----------

@app.post("/api/v1/admin/books", response_model=Book, status_code=201)
async def create_book(
    body: CreateBookRequest,
    request: Request,
    _: dict = Depends(require_role("MANAGER")),
):
    if not await storage.get_author(body.authorId):
        raise ProblemException(400, "Invalid author", f"Author {body.authorId} not found")
    if not await storage.get_genre(body.genreId):
        raise ProblemException(400, "Invalid genre", f"Genre {body.genreId} not found")
    if await storage.isbn_exists(body.isbn):
        raise ProblemException(409, "ISBN conflict", f"Book with ISBN {body.isbn} already exists")

    book = await storage.create_book(body)
    await request.app.state.broker.publish(
        "book.added",
        {
            "bookId": str(book["id"]),
            "isbn": book["isbn"],
            "title": book["title"],
            "price": book["price"],
            "currency": book["currency"],
        },
        correlation_id=get_correlation_id(request),
    )
    return book


@app.put("/api/v1/admin/books/{book_id}", response_model=Book)
async def update_book(
    book_id: uuid.UUID,
    body: UpdateBookRequest,
    request: Request,
    _: dict = Depends(require_role("MANAGER")),
):
    old = await storage.get_book(book_id)
    if not old:
        raise ProblemException(404, "Book not found", f"Book {book_id} not found")
    if body.genreId and not await storage.get_genre(body.genreId):
        raise ProblemException(400, "Invalid genre", f"Genre {body.genreId} not found")

    updated = await storage.update_book(book_id, body)
    if body.price is not None and body.price != old["price"]:
        await request.app.state.broker.publish(
            "book.price.changed",
            {
                "bookId": str(book_id),
                "oldPrice": old["price"],
                "newPrice": updated["price"],
                "currency": updated["currency"],
            },
            correlation_id=get_correlation_id(request),
        )
    return updated


@app.delete("/api/v1/admin/books/{book_id}", status_code=204)
async def delete_book(
    book_id: uuid.UUID,
    request: Request,
    _: dict = Depends(require_role("MANAGER")),
):
    if not await storage.get_book(book_id):
        raise ProblemException(404, "Book not found", f"Book {book_id} not found")
    await storage.discontinue_book(book_id)
    await request.app.state.broker.publish(
        "book.discontinued",
        {"bookId": str(book_id)},
        correlation_id=get_correlation_id(request),
    )
    return None


# ---------- Internal ----------

@app.get("/internal/v1/books/{book_id}", response_model=Book)
async def get_book_internal(book_id: uuid.UUID):
    book = await storage.get_book(book_id)
    if not book:
        raise ProblemException(404, "Book not found", f"Book {book_id} not found")
    return book


@app.get("/internal/v1/books:batch", response_model=list[Book])
async def get_books_batch(
    ids: str = Query(..., description="Comma-separated UUIDs"),
):
    try:
        id_list = [uuid.UUID(x.strip()) for x in ids.split(",") if x.strip()]
    except ValueError:
        raise ProblemException(400, "Invalid ids", "ids must be comma-separated UUIDs")
    if not id_list:
        raise ProblemException(400, "Empty ids", "At least one id is required")
    return await storage.get_books_by_ids(id_list)
