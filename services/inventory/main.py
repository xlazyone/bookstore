import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, Header, Query, Request

from shared.auth import require_role
from shared.broker import Broker, DEFAULT_URL
from shared.correlation import CorrelationIdMiddleware, get_correlation_id
from shared.idempotency import (
    get_idempotent_response, init_idempotency_table, store_idempotent_response,
)
from shared.problem import ProblemException, install_problem_handler

import storage
from models import (
    CreateReservationRequest, PaginatedStock, Reservation, StockItem, UpdateStockRequest,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("inventory-service")

QUEUE_NAME = "inventory.order.events"


async def _ttl_loop(broker: Broker, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            expired = await storage.expire_overdue()
            for res in expired:
                await broker.publish(
                    "stock.reservation.expired",
                    {"reservationId": res["reservationId"], "orderId": res["orderId"]},
                )
                logger.info("Reservation %s expired", res["orderId"])
        except Exception:
            logger.exception("TTL loop error")
        try:
            await asyncio.wait_for(stop.wait(), timeout=30.0)
        except asyncio.TimeoutError:
            pass


async def _on_order_confirmed(broker: Broker, envelope: dict) -> None:
    order_id = envelope["payload"].get("orderId")
    if not order_id:
        return
    res = await storage.confirm_reservation(order_id)
    if res:
        logger.info("Reservation %s confirmed by order.confirmed", order_id)


async def _on_order_cancelled(broker: Broker, envelope: dict) -> None:
    order_id = envelope["payload"].get("orderId")
    if not order_id:
        return
    res = await storage.release_reservation(order_id, new_status="RELEASED")
    if res:
        await broker.publish(
            "stock.released",
            {"reservationId": res["reservationId"], "orderId": order_id},
            correlation_id=envelope.get("correlationId"),
        )
        logger.info("Reservation %s released by %s", order_id, envelope.get("eventType"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    await storage.init_db()
    await storage.seed_if_empty()

    conn = await storage.get_conn()
    await init_idempotency_table(conn)

    broker = Broker(url=os.getenv("RABBITMQ_URL", DEFAULT_URL))
    await broker.connect()
    app.state.broker = broker

    async def order_event_handler(envelope: dict) -> None:
        event_type = envelope.get("eventType")
        if event_type == "order.confirmed":
            await _on_order_confirmed(broker, envelope)
        elif event_type in ("order.cancelled", "order.expired"):
            await _on_order_cancelled(broker, envelope)
        else:
            logger.warning("Unknown order event: %s", event_type)

    await broker.subscribe(
        QUEUE_NAME,
        ["order.confirmed", "order.cancelled", "order.expired"],
        order_event_handler,
    )

    stop = asyncio.Event()
    ttl_task = asyncio.create_task(_ttl_loop(broker, stop))
    app.state.ttl_stop = stop
    app.state.ttl_task = ttl_task

    logger.info("inventory-service started")
    yield

    stop.set()
    try:
        await ttl_task
    except Exception:
        pass
    await broker.close()
    await storage.close_db()
    logger.info("inventory-service stopped")


app = FastAPI(title="inventory-service", version="1.0.0", lifespan=lifespan)
app.add_middleware(CorrelationIdMiddleware)
install_problem_handler(app)


# ---------- Public ----------

@app.get("/api/v1/books/{book_id}/stock", response_model=StockItem)
async def get_public_stock(book_id: uuid.UUID):
    stock = await storage.get_stock(book_id)
    if not stock:
        raise ProblemException(404, "Stock not found", f"No stock for book {book_id}")
    return stock


# ---------- Admin ----------

@app.get("/api/v1/admin/stock", response_model=PaginatedStock)
async def list_stock(
    lowStock: bool = False,
    page: int = Query(0, ge=0),
    size: int = Query(20, ge=1, le=100),
    _: dict = Depends(require_role("MANAGER")),
):
    return await storage.list_stock(lowStock, page, size)


@app.put("/api/v1/admin/stock/{book_id}", response_model=StockItem)
async def update_stock(
    book_id: uuid.UUID,
    body: UpdateStockRequest,
    _: dict = Depends(require_role("MANAGER")),
):
    try:
        return await storage.update_stock(book_id, body.delta)
    except ValueError as e:
        if str(e) == "not_found":
            raise ProblemException(404, "Stock not found", f"No stock for book {book_id}")
        raise ProblemException(409, "Stock conflict", "Resulting stock would be negative")


# ---------- Internal ----------

@app.post("/internal/v1/reservations", response_model=Reservation, status_code=201)
async def create_reservation(
    body: CreateReservationRequest,
    request: Request,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
):
    conn = await storage.get_conn()

    # 1. Идемпотентность по Idempotency-Key
    cached = await get_idempotent_response(conn, idempotency_key)
    if cached:
        return cached[1]

    # 2. Идемпотентность по orderId
    existing = await storage.get_reservation_by_order(body.orderId)
    if existing and existing["status"] == "ACTIVE":
        await store_idempotent_response(conn, idempotency_key, 201, existing)
        return existing

    items = [{"bookId": i.bookId, "quantity": i.quantity} for i in body.items]
    try:
        reservation = await storage.create_reservation(body.orderId, items)
    except ValueError as e:
        msg = str(e)
        if msg == "insufficient":
            raise ProblemException(409, "Insufficient stock",
                                   "Not enough available copies for one or more books")
        if msg == "book_not_found":
            raise ProblemException(422, "Book not found", "One of bookIds has no stock record")
        raise

    await request.app.state.broker.publish(
        "stock.reserved",
        {"reservationId": reservation["reservationId"], "orderId": reservation["orderId"]},
        correlation_id=get_correlation_id(request),
    )
    await store_idempotent_response(conn, idempotency_key, 201, reservation)
    return reservation


@app.delete("/internal/v1/reservations/{order_id}", status_code=204)
async def release_reservation(order_id: uuid.UUID, request: Request):
    res = await storage.release_reservation(order_id, new_status="RELEASED")
    if not res:
        raise ProblemException(404, "Reservation not found", f"No reservation for order {order_id}")
    if res["status"] == "RELEASED":
        await request.app.state.broker.publish(
            "stock.released",
            {"reservationId": res["reservationId"], "orderId": res["orderId"]},
            correlation_id=get_correlation_id(request),
        )
    return None
