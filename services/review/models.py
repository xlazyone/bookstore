import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class CreateReviewRequest(BaseModel):
    rating: int = Field(ge=1, le=5)
    text: Optional[str] = Field(default=None, max_length=2000)


class ModerateReviewRequest(BaseModel):
    action: str = Field(pattern="^(APPROVE|REJECT)$")
    reason: Optional[str] = Field(default=None, max_length=500)


class Review(BaseModel):
    id: uuid.UUID
    bookId: uuid.UUID
    userId: uuid.UUID
    userName: str
    rating: int
    text: Optional[str] = None
    status: str
    verifiedPurchase: bool
    createdAt: datetime
    moderatedAt: Optional[datetime] = None


class Rating(BaseModel):
    bookId: uuid.UUID
    averageRating: float
    totalReviews: int


class PaginatedReviews(BaseModel):
    content: list[Review]
    page: int
    size: int
    totalElements: int
    totalPages: int
