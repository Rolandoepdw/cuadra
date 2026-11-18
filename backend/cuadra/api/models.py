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
    telegram_peer_id: str | None = None
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
    telegram_peer_id: str | None = None
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
    commission_type: Literal["FIXED", "PERCENT"] = "FIXED"
    stock: int | None = None
    status: Literal["ACTIVE", "OUT_OF_STOCK", "PAUSED", "REMOVED"] = "ACTIVE"
    specifications: dict = Field(default_factory=dict)
    # Empty = inherit the supplier's zones. Non-empty = restrict this product to
    # the intersection with the supplier's coverage.
    delivery_zone_ids: list[int] = Field(default_factory=list)


class ProductPatch(BaseModel):
    category_id: int | None = None
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = None
    price: float | None = Field(default=None, ge=0)
    currency: Literal["USD", "CUP"] | None = None
    commission: float | None = Field(default=None, ge=0)
    commission_currency: Literal["USD", "CUP"] | None = None
    commission_type: Literal["FIXED", "PERCENT"] | None = None
    stock: int | None = None
    status: Literal["ACTIVE", "OUT_OF_STOCK", "PAUSED", "REMOVED"] | None = None
    specifications: dict | None = None
    delivery_zone_ids: list[int] | None = None


class MainImageIn(BaseModel):
    image_id: int


class MutationReasonIn(BaseModel):
    """Operator note for approving, rejecting or reverting a mutation."""

    reason: str = Field(default="", max_length=500)


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


class DeliverySettingsIn(BaseModel):
    """H-02: how the bot describes transport when quoting a price."""

    transport_policy: Literal["coordinated", "included", "not_included"]


class SalesSettingsIn(BaseModel):
    """Fase 3 pending: operator-selectable sales mode."""

    mode: Literal["assisted", "hybrid", "autonomous"]


class FollowupSettingsIn(BaseModel):
    """`sale_followup` v2: off, shadow (log only) or on."""

    mode: Literal["off", "shadow", "on"]


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


# ------------------------------------------------------------------- customers


class CustomerPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    display_name: str | None = Field(default=None, max_length=120)
    status: Literal["ACTIVE", "BLOCKED", "SPAM"] | None = None
    usual_zone_id: int | None = None
    usual_address: str | None = Field(default=None, max_length=300)
    phone: str | None = Field(default=None, max_length=40)
    notes: str | None = Field(default=None, max_length=2000)


class CustomerTagIn(BaseModel):
    tag: str = Field(min_length=1, max_length=40)
    reason: str | None = Field(default=None, max_length=200)


class CustomerNoteIn(BaseModel):
    body: str = Field(min_length=1, max_length=2000)


class CustomerMergeIn(BaseModel):
    from_id: int = Field(ge=1)
    into_id: int = Field(ge=1)
    reason: str | None = Field(default=None, max_length=500)


class CustomerSettingsIn(BaseModel):
    """Privacy policy for the Clients panel."""

    message_retention_days: int = Field(ge=1, le=3650)
