import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class StockItem(BaseModel):
    bookId: uuid.UUID
    available: int
    reserved: int
    total: int
    updatedAt: datetime


class PaginatedStock(BaseModel):
    content: list[StockItem]
    page: int
    size: int
    totalElements: int
    totalPages: int


class UpdateStockRequest(BaseModel):
    delta: int
    reason: str = Field(pattern="^(RESTOCK|LOSS|CORRECTION)$")


class ReservationItem(BaseModel):
    bookId: uuid.UUID
    quantity: int = Field(ge=1, le=99)


class CreateReservationRequest(BaseModel):
    orderId: uuid.UUID
    items: list[ReservationItem]


class Reservation(BaseModel):
    reservationId: uuid.UUID
    orderId: uuid.UUID
    status: str
    items: list[ReservationItem]
    expiresAt: datetime
