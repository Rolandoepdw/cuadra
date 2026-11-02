"""Pydantic request/response models for the dashboard API."""

from typing import Literal

from pydantic import BaseModel, Field


class CategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    active: bool = True


class CategoryPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    active: bool | None = None


class ZoneIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    active: bool = True


class ZonePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    active: bool | None = None


class SupplierIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    business_alias: str | None = None
    whatsapp_phone: str | None = None
    telegram_id: str | None = None
    delivery_zone: str | None = None
    delivery_zone_ids: list[int] = Field(default_factory=list)
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
    delivery_zone_ids: list[int] | None = None
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


# ------------------------------------------------------------------ broadcasts


class BroadcastGroupIn(BaseModel):
    channel: Literal["telegram", "whatsapp"] = "telegram"
    target: str = Field(min_length=1, max_length=255)
    name: str = Field(min_length=1, max_length=120)
    zone_ids: list[int] = Field(default_factory=list)
    sender: Literal["bot", "userbot"] | None = None
    status: Literal["ACTIVE", "PAUSED", "BLOCKED"] = "ACTIVE"
    max_posts_per_day: int | None = Field(default=None, ge=0, le=1000)
    min_interval_s: int | None = Field(default=None, ge=0, le=604800)
    max_interval_s: int | None = Field(default=None, ge=0, le=604800)
    category_ids: list[int] = Field(default_factory=list)
    notes: str | None = None


class BroadcastGroupPatch(BaseModel):
    channel: Literal["telegram", "whatsapp"] | None = None
    target: str | None = Field(default=None, min_length=1, max_length=255)
    name: str | None = Field(default=None, min_length=1, max_length=120)
    zone_ids: list[int] | None = None
    sender: Literal["bot", "userbot"] | None = None
    status: Literal["ACTIVE", "PAUSED", "BLOCKED"] | None = None
    max_posts_per_day: int | None = Field(default=None, ge=0, le=1000)
    min_interval_s: int | None = Field(default=None, ge=0, le=604800)
    max_interval_s: int | None = Field(default=None, ge=0, le=604800)
    category_ids: list[int] | None = None
    notes: str | None = None


class BroadcastStrategyIn(BaseModel):
    max_posts_per_day: int = Field(ge=0, le=1000)
    min_interval_s: int = Field(ge=0, le=604800)
    max_interval_s: int = Field(ge=0, le=604800)
    repeat_passes: int = Field(ge=1, le=100)
    repeat_window_days: int = Field(ge=1, le=30)


class BroadcastIn(BaseModel):
    channel: Literal["telegram", "whatsapp"] = "telegram"
    template: str = Field(min_length=1, max_length=4096)
    product_id: int | None = None
    group_ids: list[int] = Field(default_factory=list)


class BroadcastPatch(BaseModel):
    # `enabled` is the reversible per-campaign publish toggle. Operational
    # `status` is owned by the scheduler and never set by a client.
    enabled: bool | None = None
    template: str | None = Field(default=None, min_length=1, max_length=4096)
    product_id: int | None = None
    group_ids: list[int] = Field(default_factory=list)
    channel: Literal["telegram", "whatsapp"] | None = None


class BroadcastPublishingIn(BaseModel):
    publishing: bool
