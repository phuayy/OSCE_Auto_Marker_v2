from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.core.webhook_url import MAX_WEBHOOK_URL_LENGTH
from app.domain.notifications import EVENT_TYPE_WILDCARD, SUBSCRIBABLE_EVENT_TYPES


WEBHOOK_DESCRIPTION_MAX_LENGTH = 255
MAX_EVENT_TYPES = len(SUBSCRIBABLE_EVENT_TYPES) + 1  # + the wildcard


class WebhookPayload(BaseModel):
    """Create/update body for a webhook subscription.

    Note what is *not* here: the signing secret. It is generated server-side and
    returned once on create, so a client can never set it to a guessable value.

    URL reachability/SSRF checks are not done here either — that needs DNS
    resolution and the runtime allow-private setting, so it lives in the service
    layer where those are available. This schema enforces only shape.
    """

    url: str = Field(min_length=1, max_length=MAX_WEBHOOK_URL_LENGTH)
    description: str = Field(default="", max_length=WEBHOOK_DESCRIPTION_MAX_LENGTH)
    eventTypes: list[str] = Field(default_factory=list, max_length=MAX_EVENT_TYPES)
    active: bool = True

    @field_validator("url", mode="before")
    @classmethod
    def trim_url(cls, value: Any) -> str:
        cleaned = str(value or "").strip()
        if not cleaned:
            raise ValueError("Webhook URL is required.")
        return cleaned[:MAX_WEBHOOK_URL_LENGTH]

    @field_validator("description", mode="before")
    @classmethod
    def trim_description(cls, value: Any) -> str:
        return str(value or "").strip()[:WEBHOOK_DESCRIPTION_MAX_LENGTH]

    @field_validator("eventTypes", mode="before")
    @classmethod
    def clean_event_types(cls, value: Any) -> list[str]:
        """Keep known types (and the wildcard), deduped, order preserved.

        An unknown type is rejected rather than dropped: silently ignoring it
        would leave the operator believing they subscribed to something they did
        not, and the endpoint would simply never fire.
        """
        if value in (None, ""):
            return []
        if not isinstance(value, list):
            raise ValueError("eventTypes must be a list.")

        allowed = {EVENT_TYPE_WILDCARD, *SUBSCRIBABLE_EVENT_TYPES}
        seen: set[str] = set()
        cleaned: list[str] = []
        for raw in value:
            item = str(raw or "").strip()
            if not item or item in seen:
                continue
            if item not in allowed:
                raise ValueError(
                    f"Unknown event type '{item}'. Valid types: "
                    f"{', '.join(sorted(allowed))}."
                )
            seen.add(item)
            cleaned.append(item)
        # The wildcard subsumes everything else; collapse so the stored filter
        # says exactly one thing.
        if EVENT_TYPE_WILDCARD in cleaned:
            return [EVENT_TYPE_WILDCARD]
        return cleaned


class WebhookResponse(BaseModel):
    webhook: dict[str, object]


class WebhookListResponse(BaseModel):
    webhooks: list[dict[str, object]]
    eventTypes: list[str]


class WebhookDeliveriesResponse(BaseModel):
    deliveries: list[dict[str, object]]


class WebhookTestResponse(BaseModel):
    delivered: bool
