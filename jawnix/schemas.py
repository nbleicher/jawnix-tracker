from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from .states import normalize_states


class SessionExchange(BaseModel):
    access_token: str = Field(min_length=20)


class ProfileUpdate(BaseModel):
    first_name: str = Field(default="", max_length=120)
    last_name: str = Field(default="", max_length=120)
    phone: str = Field(default="", max_length=40)
    licensed_states: list[str]

    @field_validator("licensed_states")
    @classmethod
    def valid_states(cls, value: list[str]) -> list[str]:
        return normalize_states(value)


class RequestCreate(BaseModel):
    lead_count: int = Field(ge=1, le=100_000)
    state_mode: str = Field(default="all_saved", pattern="^(all_saved|selected)$")
    states: list[str] = Field(default_factory=list)

    @field_validator("states")
    @classmethod
    def valid_states(cls, value: list[str]) -> list[str]:
        return normalize_states(value)

    @model_validator(mode="after")
    def selected_requires_states(self):
        if self.state_mode == "selected" and not self.states:
            raise ValueError("Select at least one state for this request.")
        return self


class RecipientMappingUpdate(BaseModel):
    agent_id: int
    confirmed: bool = True


class CustomerCreate(BaseModel):
    email: EmailStr
    first_name: str = Field(default="", max_length=120)
    last_name: str = Field(default="", max_length=120)
    password: str = Field(min_length=8, max_length=256)


class ProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    user_id: uuid.UUID
    email: str
    first_name: str
    last_name: str
    phone: str
    licensed_states: list[str]
    agent_id: int | None
    mapping_confirmed_at: datetime | None


class RequestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    lead_count: int
    state_mode: str
    states_snapshot: list[str]
    status: str
    available_count: int | None
    status_message: str
    created_at: datetime
    delivered_at: datetime | None

