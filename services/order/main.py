import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from fastapi import Depends, FastAPI, Header, Query, Request

from shared.auth import get_current_user
from shared.broker import Broker, DEFAULT_URL
from shared.correlation import CorrelationIdMiddleware, get_correlation_id
from shared.idempotency import (
    get_idempotent_response, init_idempotency_table, store_idempotent_response,
)
from shared.problem import ProblemException, install_problem_handler

import storage
from models import (
    AddCartItemRequest, Cart, CreateOrderRequest, Order, PaginatedOrders,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("order-service")

CATALOG_URL = os.getenv("CATALOG_URL", "http://catalog-service:8001")
INVENTORY_URL = os.getenv("INVENTORY_URL", "http://inventory-service:8002")
PAYMENT_URL = os.getenv("PAYMENT_URL", "http://payment-service:8004")
QUEUE_PAYMENT = "order.payment.events"


def _problem_from_http(response: httpx.Response, service: str) -> ProblemException:
    try:
        body = response.json()
    except Exception:
        body = {}
    return ProblemException(
        status=502,
        title=f"{service} error",
        detail=body.get("title") or body.get("detail") or f"{service} returned {response.status_code}",
        type_="https://bookstore.local/errors/upstream",
    )


# ---------- Saga ----------

async def _orchestrate_saga(app, user_id: str, order_id: str, items: list[dict],
                             total: float, currency: str, simulate: str,
                             correlation_id: str) -> None:
    """Асинхронная часть саги: reserve -> payment -> опубликовать order.created.
    При сбое — компенсации в обратном порядке и order.cancelled."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        # 1. Reserve
        try:
            r = await client.post(
                f"{INVENTORY_URL}/internal/v1/reservations",
                json={
                    "orderId": order_id,
                    "items": [{"bookId": i["bookId"], "quantity": i["quantity"]} for i in items],
                },
                headers={
                    "Idempotency-Key": f"order-{order_id}-reserve",
                    "X-Correlation-Id": correlation_id,
                },
            )
            if r.status_code >= 400:
                raise _problem_from_http(r, "inventory")
        except Exception as exc:
            logger.exception("Reserve failed for order %s", order_id)
            await storage.update_order_status(order_id, "CANCELLED")
            await app.state.broker.publish(
                "order.cancelled",
                {"orderId": order_id, "reason": "RESERVE_FAILED"},
                correlation_id=correlation_id,
            )
            return

        # 2. Payment
        try:
            r = await client.post(
                f"{PAYMENT_URL}/internal/v1/payments",
                json={
                    "orderId": order_id,
                    "amount": total,
                    "currency": currency,
                    "simulate": simulate,
                },
                headers={
                    "Idempotency-Key": f"order-{order_id}-payment",
                    "X-Correlation-Id": correlation_id,
                },
            )
            if r.status_code >= 400:
                raise _problem_from_http(r, "payment")
            psp = r.json()
        except Exception as exc:
            logger.exception("Payment creation failed for order %s", order_id)
            # компенсация резерва
            try:
                await client.delete(
                    f"{INVENTORY_URL}/internal/v1/reservations/{order_id}",
                    headers={"X-Correlation-Id": correlation_id},
                )
            except Exception:
                logger.exception("Compensation (release reservation) failed for %s", order_id)
            await storage.update_order_status(order_id, "CANCELLED")
            await app.state.broker.publish(
                "order.cancelled",
                {"orderId": order_id, "reason": "PAYMENT_INIT_FAILED"},
                correlation_id=correlation_id,
            )
            return

    await storage.set_payment_url(order_id, psp.get("paymentUrl"))
    await app.state.broker.publish(
        "order.created",
        {
            "orderId": order_id, "userId": user_id,
            "totalAmount": total, "currency": currency,
            "expiresAt": None,
        },
        correlation_id=correlation_id,
    )
    logger.info("Saga started for order %s (payment %s)", order_id, psp.get("paymentId"))


async def _on_payment_completed(app, envelope: dict) -> None:
    payload = envelope.get("payload", {})
    order_id = payload.get("orderId")
    if not order_id:
        return
    order = await storage.get_order(order_id)
    if not order or order["status"] != "PENDING_PAYMENT":
        return
    await storage.update_order_status(order_id, "CONFIRMED")
    await app.state.broker.publish(
        "order.confirmed",
        {
            "orderId": order_id,
            "userId": order["userId"],
            "userEmail": f"user-{order['userId']}@example.com",
            "totalAmount": order["totalAmount"],
            "currency": order["currency"],
            "items": [{"bookId": i["bookId"], "quantity": i["quantity"]} for i in order["items"]],
        },
        correlation_id=envelope.get("correlationId"),
    )
    await storage.clear_cart(order["userId"])
    logger.info("Order %s confirmed", order_id)


async def _on_payment_failed(app, envelope: dict) -> None:
    payload = envelope.get("payload", {})
    order_id = payload.get("orderId")
    if not order_id:
        return
    order = await storage.get_order(order_id)
    if not order or order["status"] != "PENDING_PAYMENT":
        return
    await storage.update_order_status(order_id, "CANCELLED")
    await app.state.broker.publish(
        "order.cancelled",
        {"orderId": order_id, "reason": "PAYMENT_FAILED"},
        correlation_id=envelope.get("correlationId"),
    )
    logger.info("Order %s cancelled (payment failed)", order_id)


async def _on_payment_refunded(app, envelope: dict) -> None:
    payload = envelope.get("payload", {})
    order_id = payload.get("orderId")
    if not order_id:
        return
    order = await storage.get_order(order_id)
    if not order:
        return
    if order["status"] == "CONFIRMED":
        await storage.update_order_status(order_id, "CANCELLED")
    logger.info("Order %s refunded", order_id)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await storage.init_db()
    conn = await storage.get_conn()
    await init_idempotency_table(conn)

    broker = Broker(url=os.getenv("RABBITMQ_URL", DEFAULT_URL))
    await broker.connect()
    app.state.broker = broker

    async def completed_handler(env): await _on_payment_completed(app, env)
    async def failed_handler(env): await _on_payment_failed(app, env)
    async def refunded_handler(env): await _on_payment_refunded(app, env)

    await broker.subscribe(QUEUE_PAYMENT, ["payment.completed"], completed_handler)
    await broker.subscribe(QUEUE_PAYMENT, ["payment.failed"], failed_handler)
    await broker.subscribe(QUEUE_PAYMENT, ["payment.refunded"], refunded_handler)

    logger.info("order-service started")
    yield
    await broker.close()
    await storage.close_db()
    logger.info("order-service stopped")


app = FastAPI(title="order-service", version="1.0.0", lifespan=lifespan)
app.add_middleware(CorrelationIdMiddleware)
install_problem_handler(app)


# ---------- Cart ----------

@app.get("/api/v1/cart", response_model=Cart)
async def get_cart(user: dict = Depends(get_current_user)):
    return await storage.get_cart(user["userId"])


@app.post("/api/v1/cart/items", response_model=Cart)
async def add_cart_item(
    body: AddCartItemRequest,
    request: Request,
    user: dict = Depends(get_current_user),
):
    # получаем данные книги из catalog
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            f"{CATALOG_URL}/internal/v1/books/{body.bookId}",
            headers={"X-Correlation-Id": get_correlation_id(request)},
        )
    if r.status_code == 404:
        raise ProblemException(404, "Book not found", f"Book {body.bookId} not found")
    if r.status_code >= 400:
        raise _problem_from_http(r, "catalog")
    book = r.json()
    return await storage.add_cart_item(user["userId"], book, body.quantity)


@app.delete("/api/v1/cart/items/{item_id}", status_code=204)
async def remove_cart_item(item_id: uuid.UUID, user: dict = Depends(get_current_user)):
    if not await storage.remove_cart_item(user["userId"], item_id):
        raise ProblemException(404, "Cart item not found", f"Item {item_id} not found")
    return None


# ---------- Orders ----------

@app.post("/api/v1/orders", response_model=Order, status_code=201)
async def create_order(
    body: CreateOrderRequest,
    request: Request,
    user: dict = Depends(get_current_user),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
):
    conn = await storage.get_conn()
    cached = await get_idempotent_response(conn, idempotency_key)
    if cached:
        return cached[1]

    cart = await storage.get_cart(user["userId"])
    if not cart["items"]:
        raise ProblemException(422, "Cart is empty", "Cannot create order from empty cart")

    # актуализируем цены/названия через catalog batch
    ids = ",".join(i["bookId"] for i in cart["items"])
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            f"{CATALOG_URL}/internal/v1/books:batch",
            params={"ids": ids},
            headers={"X-Correlation-Id": get_correlation_id(request)},
        )
    if r.status_code >= 400:
        raise _problem_from_http(r, "catalog")
    catalog_books = {b["id"]: b for b in r.json()}

    items = []
    for ci in cart["items"]:
        cb = catalog_books.get(ci["bookId"])
        if not cb or not cb.get("active", True):
            raise ProblemException(422, "Book unavailable",
                                   f"Book {ci['bookId']} is not available for purchase")
        items.append({
            "bookId": ci["bookId"],
            "title": cb["title"],
            "price": cb["price"],
            "quantity": ci["quantity"],
        })

    order = await storage.create_order(user["userId"], items, body.currency)

    await _orchestrate_saga(
        app, user["userId"], order["id"], items,
        order["totalAmount"], order["currency"], body.simulatePayment,
        get_correlation_id(request),
    )

    final = await storage.get_order(order["id"])
    await store_idempotent_response(conn, idempotency_key, 201, final)
    return final


@app.get("/api/v1/orders", response_model=PaginatedOrders)
async def list_orders(
    page: int = Query(0, ge=0),
    size: int = Query(20, ge=1, le=100),
    user: dict = Depends(get_current_user),
):
    return await storage.list_orders(user["userId"], page, size)


@app.get("/api/v1/orders/{order_id}", response_model=Order)
async def get_order(order_id: uuid.UUID, user: dict = Depends(get_current_user)):
    order = await storage.get_order_by_status_user(user["userId"], order_id)
    if not order:
        raise ProblemException(404, "Order not found", f"Order {order_id} not found")
    return order


@app.post("/api/v1/orders/{order_id}/cancel", response_model=Order)
async def cancel_order(order_id: uuid.UUID, request: Request, user: dict = Depends(get_current_user)):
    order = await storage.get_order_by_status_user(user["userId"], order_id)
    if not order:
        raise ProblemException(404, "Order not found", f"Order {order_id} not found")
    if order["status"] != "PENDING_PAYMENT":
        raise ProblemException(409, "Cannot cancel order",
                               f"Order is in status {order['status']}")

    # компенсация резерва
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            await client.delete(
                f"{INVENTORY_URL}/internal/v1/reservations/{order_id}",
                headers={"X-Correlation-Id": get_correlation_id(request)},
            )
        except Exception:
            logger.exception("Release reservation failed for order %s", order_id)

    updated = await storage.update_order_status(order_id, "CANCELLED")
    await request.app.state.broker.publish(
        "order.cancelled",
        {"orderId": str(order_id), "reason": "USER_CANCELLED"},
        correlation_id=get_correlation_id(request),
    )
    return updated


# ---------- Internal ----------

@app.get("/internal/v1/orders/{order_id}", response_model=Order)
async def get_order_internal(order_id: uuid.UUID):
    order = await storage.get_order(order_id)
    if not order:
        raise ProblemException(404, "Order not found", f"Order {order_id} not found")
    return order
