import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class Payment(BaseModel):
    id: uuid.UUID
    orderId: uuid.UUID
    amount: float
    currency: str
    status: str
    paymentUrl: Optional[str] = None
    createdAt: datetime
    updatedAt: datetime


class CreatePaymentRequest(BaseModel):
    orderId: uuid.UUID
    amount: float = Field(gt=0)
    currency: str = "RUB"
    simulate: str = Field(default="SUCCESS", pattern="^(SUCCESS|FAIL)$",
                          description="Для учебного мока PSP: SUCCESS|FAIL")


class Refund(BaseModel):
    id: uuid.UUID
    paymentId: uuid.UUID
    amount: float
    status: str
    reason: Optional[str] = None
    createdAt: datetime


class CreateRefundRequest(BaseModel):
    paymentId: uuid.UUID
    amount: float = Field(gt=0)
    reason: str


class WebhookPayload(BaseModel):
    eventId: uuid.UUID
    paymentId: uuid.UUID
    status: str = Field(pattern="^(SUCCEEDED|FAILED)$")
    amount: float
    occurredAt: Optional[str] = None
