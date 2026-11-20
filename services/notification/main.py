import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import Optional

import aiosmtplib
from email.message import EmailMessage
from fastapi import FastAPI, Query, Request

from shared.broker import Broker, DEFAULT_URL
from shared.correlation import CorrelationIdMiddleware
from shared.problem import ProblemException, install_problem_handler

import storage
import templates
from models import Notification, PaginatedNotifications

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("notification-service")

QUEUE_NAME = "notification.events"
SMTP_HOST = os.getenv("SMTP_HOST", "mailhog")
SMTP_PORT = int(os.getenv("SMTP_PORT", "1025"))
SMTP_FROM = os.getenv("SMTP_FROM", "noreply@bookstore.local")


async def _send_email(recipient: str, subject: str, body: str) -> str:
    msg = EmailMessage()
    msg["From"] = SMTP_FROM
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.set_content(body)
    await aiosmtplib.send(msg, hostname=SMTP_HOST, port=SMTP_PORT, timeout=10)
    return f"<mailhog-{uuid.uuid4().hex[:8]}>"


async def _handle_event(broker: Broker, envelope: dict) -> None:
    event_type = envelope.get("eventType")
    payload = envelope.get("payload", {})
    renderer = templates.RENDERERS.get(event_type)
    if not renderer:
        logger.debug("No renderer for %s, skip", event_type)
        return

    order_id = payload.get("orderId")
    recipient = payload.get("userEmail") or f"user-{payload.get('userId', 'unknown')}@example.com"
    notif_type = templates.NOTIFICATION_TYPES.get(event_type, "UNKNOWN")
    subject, body = renderer(payload)

    notif = await storage.create_notification(recipient, notif_type, subject, payload, order_id)
    try:
        provider_id = await _send_email(recipient, subject, body)
        await storage.mark_sent(notif["id"], provider_id)
        await broker.publish(
            "notification.sent",
            {"notificationId": notif["id"], "type": notif_type, "recipient": recipient},
            correlation_id=envelope.get("correlationId"),
        )
        logger.info("Sent %s to %s", notif_type, recipient)
    except Exception as exc:
        await storage.mark_failed(notif["id"], str(exc))
        await broker.publish(
            "notification.failed",
            {"notificationId": notif["id"], "type": notif_type, "error": str(exc)[:200]},
            correlation_id=envelope.get("correlationId"),
        )
        logger.exception("Failed to send %s to %s", notif_type, recipient)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await storage.init_db()
    broker = Broker(url=os.getenv("RABBITMQ_URL", DEFAULT_URL))
    await broker.connect()
    app.state.broker = broker

    async def handler(env): await _handle_event(broker, env)

    await broker.subscribe(
        QUEUE_NAME,
        [
            "order.confirmed", "order.cancelled", "order.expired",
            "payment.completed", "payment.failed",
            "review.published",
        ],
        handler,
    )
    logger.info("notification-service started")
    yield
    await broker.close()
    await storage.close_db()
    logger.info("notification-service stopped")


app = FastAPI(title="notification-service", version="1.0.0", lifespan=lifespan)
app.add_middleware(CorrelationIdMiddleware)
install_problem_handler(app)


@app.get("/internal/v1/notifications", response_model=PaginatedNotifications)
async def list_notifications(
    orderId: Optional[uuid.UUID] = None,
    status: Optional[str] = Query(None, pattern="^(QUEUED|SENT|FAILED)$"),
    page: int = Query(0, ge=0),
    size: int = Query(20, ge=1, le=100),
):
    return await storage.list_notifications(str(orderId) if orderId else None, status, page, size)


@app.get("/internal/v1/notifications/{notif_id}", response_model=Notification)
async def get_notification(notif_id: uuid.UUID):
    n = await storage.get_notification(notif_id)
    if not n:
        raise ProblemException(404, "Notification not found", f"Notification {notif_id} not found")
    return n


@app.post("/internal/v1/notifications/{notif_id}/retry", status_code=202)
async def retry_notification(notif_id: uuid.UUID, request: Request):
    n = await storage.get_notification(notif_id)
    if not n:
        raise ProblemException(404, "Notification not found", f"Notification {notif_id} not found")
    if n["status"] == "SENT":
        raise ProblemException(409, "Already sent", "Notification is already SENT")

    try:
        provider_id = await _send_email(
            n["recipientEmail"],
            n["subject"] or "BookStore",
            f"Повторная отправка: {n['type']}",
        )
        await storage.mark_sent(notif_id, provider_id)
        await request.app.state.broker.publish(
            "notification.sent",
            {"notificationId": str(notif_id), "type": n["type"],
             "recipient": n["recipientEmail"]},
        )
        return {"status": "sent"}
    except Exception as exc:
        await storage.mark_failed(notif_id, str(exc))
        raise ProblemException(502, "SMTP error", str(exc)[:200])
