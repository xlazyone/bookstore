import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class AddCartItemRequest(BaseModel):
    bookId: uuid.UUID
    quantity: int = Field(ge=1, le=99)


class CartItem(BaseModel):
    itemId: uuid.UUID
    bookId: uuid.UUID
    title: str
    price: float
    quantity: int


class Cart(BaseModel):
    userId: uuid.UUID
    items: list[CartItem]
    totalAmount: float
    currency: str = "RUB"


class CreateOrderRequest(BaseModel):
    currency: str = "RUB"
    comment: Optional[str] = Field(default=None, max_length=500)
    simulatePayment: str = Field(default="SUCCESS", pattern="^(SUCCESS|FAIL)$",
                                 description="Для демо: SUCCESS|FAIL — прокинется в payment-service")


class OrderItem(BaseModel):
    bookId: uuid.UUID
    title: str
    price: float
    quantity: int


class Order(BaseModel):
    id: uuid.UUID
    userId: uuid.UUID
    status: str
    items: list[OrderItem]
    totalAmount: float
    currency: str
    paymentUrl: Optional[str] = None
    expiresAt: Optional[datetime] = None
    createdAt: datetime
    updatedAt: datetime


class OrderSummary(BaseModel):
    id: uuid.UUID
    status: str
    totalAmount: float
    currency: str
    createdAt: datetime


class PaginatedOrders(BaseModel):
    content: list[OrderSummary]
    page: int
    size: int
    totalElements: int
    totalPages: int
