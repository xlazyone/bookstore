import asyncio
import hashlib
import hmac
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import FastAPI, Request
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("mock-payment-gateway")

WEBHOOK_URL = os.getenv("PAYMENT_WEBHOOK_URL", "http://payment-service:8004/api/v1/payments/webhook")
WEBHOOK_SECRET = os.getenv("PAYMENT_WEBHOOK_SECRET", "dev-secret-change-me")
WEBHOOK_DELAY_SECONDS = float(os.getenv("WEBHOOK_DELAY_SECONDS", "3"))

payments: dict = {}


class CreateSessionRequest(BaseModel):
    orderId: uuid.UUID
    amount: float = Field(gt=0)
    currency: str = "RUB"
    simulate: str = Field(default="SUCCESS", pattern="^(SUCCESS|FAIL)$")


class CreateSessionResponse(BaseModel):
    paymentId: uuid.UUID
    orderId: uuid.UUID
    amount: float
    currency: str
    status: str
    paymentUrl: str


async def _send_webhook_later(payment_id: str, simulate: str) -> None:
    await asyncio.sleep(WEBHOOK_DELAY_SECONDS)
    payment = payments.get(payment_id)
    if not payment:
        return
    status = "SUCCEEDED" if simulate == "SUCCESS" else "FAILED"
    payload = {
        "eventId": str(uuid.uuid4()),
        "paymentId": payment_id,
        "status": status,
        "amount": payment["amount"],
        "occurredAt": datetime.now(timezone.utc).isoformat(),
    }
    body = json.dumps(payload).encode("utf-8")
    signature = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.post(
                WEBHOOK_URL,
                content=body,
                headers={"Content-Type": "application/json", "X-Signature": signature},
            )
            logger.info("Webhook sent paymentId=%s status=%s -> %s", payment_id, status, r.status_code)
    except Exception:
        logger.exception("Failed to send webhook for %s", payment_id)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("mock-payment-gateway started, webhook=%s", WEBHOOK_URL)
    yield


app = FastAPI(title="mock-payment-gateway", version="1.0.0", lifespan=lifespan)


@app.post("/payments", response_model=CreateSessionResponse, status_code=201)
async def create_session(body: CreateSessionRequest):
    payment_id = str(uuid.uuid4())
    payments[payment_id] = {
        "paymentId": payment_id,
        "orderId": str(body.orderId),
        "amount": body.amount,
        "currency": body.currency,
        "status": "PENDING",
        "simulate": body.simulate,
        "createdAt": datetime.now(timezone.utc).isoformat(),
    }
    asyncio.create_task(_send_webhook_later(payment_id, body.simulate))
    logger.info("Session created paymentId=%s orderId=%s simulate=%s",
                payment_id, body.orderId, body.simulate)
    return CreateSessionResponse(
        paymentId=payment_id,
        orderId=body.orderId,
        amount=body.amount,
        currency=body.currency,
        status="PENDING",
        paymentUrl=f"https://mock-pay.local/session/{payment_id}",
    )
