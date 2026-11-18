import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class Author(BaseModel):
    id: uuid.UUID
    name: str
    bio: Optional[str] = None


class Genre(BaseModel):
    id: uuid.UUID
    name: str


class Book(BaseModel):
    id: uuid.UUID
    isbn: str
    title: str
    description: Optional[str] = None
    author: Author
    genre: Genre
    price: float
    currency: str = "RUB"
    coverUrl: Optional[str] = None
    active: bool = True
    createdAt: datetime
    updatedAt: datetime


class BookSummary(BaseModel):
    id: uuid.UUID
    title: str
    author: Author
    price: float
    currency: str
    coverUrl: Optional[str] = None


class CreateBookRequest(BaseModel):
    isbn: str
    title: str
    description: Optional[str] = None
    authorId: uuid.UUID
    genreId: uuid.UUID
    price: float = Field(ge=0)
    currency: str = "RUB"


class UpdateBookRequest(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    genreId: Optional[uuid.UUID] = None
    price: Optional[float] = Field(default=None, ge=0)
    active: Optional[bool] = None


class PaginatedBooks(BaseModel):
    content: list[BookSummary]
    page: int
    size: int
    totalElements: int
    totalPages: int
