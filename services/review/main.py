import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, Header, Query, Request

from shared.auth import get_current_user, require_role
from shared.broker import Broker, DEFAULT_URL
from shared.correlation import CorrelationIdMiddleware, get_correlation_id
from shared.idempotency import (
    get_idempotent_response, init_idempotency_table, store_idempotent_response,
)
from shared.problem import ProblemException, install_problem_handler

import storage
from models import (
    CreateReviewRequest, ModerateReviewRequest, PaginatedReviews, Rating, Review,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("review-service")

QUEUE_NAME = "review.order.events"


async def _on_order_confirmed(envelope: dict) -> None:
    payload = envelope.get("payload", {})
    user_id = payload.get("userId")
    items = payload.get("items", [])
    if not user_id:
        return
    for item in items:
        book_id = item.get("bookId")
        if book_id:
            await storage.add_verified_purchase(user_id, book_id)
    logger.info("Verified purchases recorded for user %s (%d items)",
                user_id, len(items))


@asynccontextmanager
async def lifespan(app: FastAPI):
    await storage.init_db()
    conn = await storage.get_conn()
    await init_idempotency_table(conn)

    broker = Broker(url=os.getenv("RABBITMQ_URL", DEFAULT_URL))
    await broker.connect()
    app.state.broker = broker

    await broker.subscribe(QUEUE_NAME, ["order.confirmed"], _on_order_confirmed)

    logger.info("review-service started")
    yield
    await broker.close()
    await storage.close_db()
    logger.info("review-service stopped")


app = FastAPI(title="review-service", version="1.0.0", lifespan=lifespan)
app.add_middleware(CorrelationIdMiddleware)
install_problem_handler(app)


# ---------- Public ----------

@app.get("/api/v1/books/{book_id}/rating", response_model=Rating)
async def get_rating(book_id: uuid.UUID):
    return await storage.get_rating(book_id)


@app.get("/api/v1/books/{book_id}/reviews", response_model=PaginatedReviews)
async def list_reviews(
    book_id: uuid.UUID,
    page: int = Query(0, ge=0),
    size: int = Query(20, ge=1, le=100),
):
    return await storage.list_published_reviews(book_id, page, size)


@app.post("/api/v1/books/{book_id}/reviews", response_model=Review, status_code=201)
async def create_review(
    book_id: uuid.UUID,
    body: CreateReviewRequest,
    request: Request,
    user: dict = Depends(get_current_user),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
):
    conn = await storage.get_conn()
    cached = await get_idempotent_response(conn, idempotency_key)
    if cached:
        return cached[1]

    existing = await storage.get_user_review(user["userId"], book_id)
    if existing:
        raise ProblemException(409, "Review already exists",
                               f"User already reviewed book {book_id}")

    user_name = f"User {user['userId'][:8]}"
    review = await storage.create_review(
        book_id, user["userId"], user_name, body.rating, body.text,
    )
    await store_idempotent_response(conn, idempotency_key, 201, review)
    logger.info("Review %s created (book=%s, user=%s)", review["id"], book_id, user["userId"])
    return review


# ---------- Admin ----------

@app.post("/api/v1/admin/reviews/{review_id}/moderate", response_model=Review)
async def moderate_review(
    review_id: uuid.UUID,
    body: ModerateReviewRequest,
    request: Request,
    _: dict = Depends(require_role("MODERATOR")),
):
    review = await storage.get_review(review_id)
    if not review:
        raise ProblemException(404, "Review not found", f"Review {review_id} not found")
    if review["status"] != "PENDING":
        raise ProblemException(409, "Review already moderated",
                               f"Review is in status {review['status']}")
    if body.action == "REJECT" and not body.reason:
        raise ProblemException(422, "Reason is required",
                               "reason must be provided when action=REJECT")

    approve = body.action == "APPROVE"
    updated = await storage.moderate_review(review_id, approve)
    if approve:
        await request.app.state.broker.publish(
            "review.published",
            {
                "reviewId": updated["id"],
                "bookId": updated["bookId"],
                "userId": updated["userId"],
                "rating": updated["rating"],
            },
            correlation_id=get_correlation_id(request),
        )
        logger.info("Review %s published, event review.published emitted", review_id)
    else:
        logger.info("Review %s rejected: %s", review_id, body.reason)
    return updated
