import hashlib
import hmac
import logging
import os
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Header, Request

from shared.broker import Broker, DEFAULT_URL
from shared.correlation import CorrelationIdMiddleware, get_correlation_id
from shared.idempotency import (
    get_idempotent_response, init_idempotency_table, store_idempotent_response,
)
from shared.problem import ProblemException, install_problem_handler

import storage
from models import (
    CreatePaymentRequest, CreateRefundRequest, Payment, Refund, WebhookPayload,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("payment-service")

PSP_URL = os.getenv("PAYMENT_GATEWAY_URL", "http://mock-payment-gateway:8080")
WEBHOOK_SECRET = os.getenv("PAYMENT_WEBHOOK_SECRET", "dev-secret-change-me")
QUEUE_NAME = "payment.order.events"


async def _on_order_expired(broker: Broker, envelope: dict) -> None:
    order_id = envelope["payload"].get("orderId")
    if not order_id:
        return
    payment = await storage.get_payment_by_order(order_id)
    if not payment:
        return
    if payment["status"] == "COMPLETED":
        existing_refund = await storage.get_refund_by_payment(payment["id"])
        if existing_refund:
            return
        refund = await storage.insert_refund(payment["id"], payment["amount"], "ORDER_EXPIRED")
        await broker.publish(
            "payment.refunded",
            {"refundId": refund["id"], "paymentId": payment["id"],
             "orderId": order_id, "amount": refund["amount"]},
            correlation_id=envelope.get("correlationId"),
        )
        logger.info("Refund %s issued for expired order %s", refund["id"], order_id)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await storage.init_db()
    conn = await storage.get_conn()
    await init_idempotency_table(conn)

    broker = Broker(url=os.getenv("RABBITMQ_URL", DEFAULT_URL))
    await broker.connect()
    app.state.broker = broker

    async def handler(env): await _on_order_expired(broker, env)
    await broker.subscribe(QUEUE_NAME, ["order.expired"], handler)

    logger.info("payment-service started")
    yield
    await broker.close()
    await storage.close_db()
    logger.info("payment-service stopped")


app = FastAPI(title="payment-service", version="1.0.0", lifespan=lifespan)
app.add_middleware(CorrelationIdMiddleware)
install_problem_handler(app)


# ---------- Internal ----------

@app.post("/internal/v1/payments", response_model=Payment, status_code=201)
async def create_payment(
    body: CreatePaymentRequest,
    request: Request,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
):
    conn = await storage.get_conn()
    cached = await get_idempotent_response(conn, idempotency_key)
    if cached:
        return cached[1]

    existing = await storage.get_payment_by_order(body.orderId)
    if existing:
        await store_idempotent_response(conn, idempotency_key, 201, existing)
        return existing

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                f"{PSP_URL}/payments",
                json={
                    "orderId": str(body.orderId),
                    "amount": body.amount,
                    "currency": body.currency,
                    "simulate": body.simulate,
                },
            )
            r.raise_for_status()
            psp = r.json()
    except httpx.HTTPError as exc:
        logger.exception("PSP error")
        raise ProblemException(502, "Payment gateway error", str(exc),
                               type_="https://bookstore.local/errors/upstream")

    payment = await storage.insert_payment(
        psp["paymentId"],
        body.orderId, body.amount, body.currency, psp.get("paymentUrl"),
    )
    await request.app.state.broker.publish(
        "payment.initiated",
        {"paymentId": payment["id"], "orderId": str(body.orderId),
         "amount": body.amount, "currency": body.currency, "status": "INITIATED"},
        correlation_id=get_correlation_id(request),
    )
    await store_idempotent_response(conn, idempotency_key, 201, payment)
    return payment


@app.post("/internal/v1/refunds", response_model=Refund, status_code=201)
async def create_refund(
    body: CreateRefundRequest,
    request: Request,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
):
    conn = await storage.get_conn()
    cached = await get_idempotent_response(conn, idempotency_key)
    if cached:
        return cached[1]

    payment = await storage.get_payment(body.paymentId)
    if not payment:
        raise ProblemException(404, "Payment not found", f"Payment {body.paymentId} not found")
    if payment["status"] != "COMPLETED":
        raise ProblemException(422, "Refund not allowed",
                               "Refund only possible for COMPLETED payments")

    existing = await storage.get_refund_by_payment(body.paymentId)
    if existing:
        await store_idempotent_response(conn, idempotency_key, 201, existing)
        return existing

    refund = await storage.insert_refund(body.paymentId, body.amount, body.reason)
    await storage.update_payment_status(body.paymentId, "REFUNDED")
    await request.app.state.broker.publish(
        "payment.refunded",
        {"refundId": refund["id"], "paymentId": str(body.paymentId),
         "orderId": payment["orderId"], "amount": body.amount},
        correlation_id=get_correlation_id(request),
    )
    await store_idempotent_response(conn, idempotency_key, 201, refund)
    return refund


# ---------- Webhook ----------

@app.post("/api/v1/payments/webhook")
async def handle_webhook(
    body: WebhookPayload,
    request: Request,
    x_signature: str = Header(..., alias="X-Signature"),
):
    raw = await request.body()
    expected = hmac.new(WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, x_signature):
        raise ProblemException(401, "Invalid signature", None,
                               type_="https://bookstore.local/errors/unauthorized")

    if await storage.is_webhook_processed(body.eventId):
        logger.info("Webhook %s already processed", body.eventId)
        return {"status": "already_processed"}

    payment = await storage.get_payment(body.paymentId)
    if not payment:
        raise ProblemException(404, "Payment not found", f"Payment {body.paymentId} not found")

    new_status = "COMPLETED" if body.status == "SUCCEEDED" else "FAILED"
    updated = await storage.update_payment_status(body.paymentId, new_status)
    await storage.mark_webhook_processed(body.eventId)

    event_type = "payment.completed" if new_status == "COMPLETED" else "payment.failed"
    await request.app.state.broker.publish(
        event_type,
        {"paymentId": str(body.paymentId), "orderId": updated["orderId"],
         "amount": updated["amount"], "currency": updated["currency"],
         "status": new_status},
        correlation_id=get_correlation_id(request),
    )
    logger.info("Webhook %s -> %s, published %s", body.eventId, new_status, event_type)
    return {"status": "ok"}


# ---------- Public ----------

@app.get("/api/v1/payments/{payment_id}", response_model=Payment)
async def get_payment(payment_id: uuid.UUID):
    p = await storage.get_payment(payment_id)
    if not p:
        raise ProblemException(404, "Payment not found", f"Payment {payment_id} not found")
    return p
