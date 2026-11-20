import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class Notification(BaseModel):
    id: uuid.UUID
    recipientEmail: str
    type: str
    status: str
    subject: Optional[str] = None
    payload: dict
    providerMessageId: Optional[str] = None
    errorMessage: Optional[str] = None
    retryCount: int = 0
    createdAt: datetime
    sentAt: Optional[datetime] = None


class PaginatedNotifications(BaseModel):
    content: list[Notification]
    page: int
    size: int
    totalElements: int
    totalPages: int
