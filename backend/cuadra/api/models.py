"""Pydantic request/response models for the dashboard API."""

from typing import Literal

from pydantic import BaseModel, Field


class CategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    active: bool = True


class CategoryPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    active: bool | None = None


class SupplierIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    business_alias: str | None = None
    whatsapp_phone: str | None = None
    telegram_id: str | None = None
    delivery_zone: str | None = None
    payment_terms: str | None = None
    priority: int = 100
    notes: str | None = None
    active: bool = True


class SupplierPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    business_alias: str | None = None
    whatsapp_phone: str | None = None
    telegram_id: str | None = None
    delivery_zone: str | None = None
    payment_terms: str | None = None
    priority: int | None = None
    notes: str | None = None
    active: bool | None = None


class ProductIn(BaseModel):
    supplier_id: int
    category_id: int | None = None
    name: str = Field(min_length=1, max_length=120)
    description: str | None = None
    price: float = Field(ge=0)
    currency: Literal["USD", "CUP"] = "USD"
    commission: float = Field(ge=0)
    commission_currency: Literal["USD", "CUP"] = "USD"
    stock: int | None = None
    status: Literal["ACTIVE", "OUT_OF_STOCK", "PAUSED", "REMOVED"] = "ACTIVE"
    specifications: dict = Field(default_factory=dict)


class ProductPatch(BaseModel):
    category_id: int | None = None
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = None
    price: float | None = Field(default=None, ge=0)
    currency: Literal["USD", "CUP"] | None = None
    commission: float | None = Field(default=None, ge=0)
    commission_currency: Literal["USD", "CUP"] | None = None
    stock: int | None = None
    status: Literal["ACTIVE", "OUT_OF_STOCK", "PAUSED", "REMOVED"] | None = None
    specifications: dict | None = None


class MainImageIn(BaseModel):
    image_id: int
