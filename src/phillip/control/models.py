"""Models for strategy registration, configuration, and deployment status."""

from __future__ import annotations

import json
import re

from pathlib import Path
from typing import Literal

from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import TraderId
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import model_validator


FieldType = Literal["string", "select"]
ValidatorType = Literal["string", "choice", "nautilus_trader_id", "nautilus_bar_type"]


class StrategyField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")
    label: str = Field(min_length=1, max_length=80)
    type: FieldType = "string"
    validator: ValidatorType = "string"
    required: bool = True
    options: list[str] = Field(default_factory=list, max_length=100)
    pattern: str | None = None
    min_length: int = Field(default=0, ge=0, le=4096)
    max_length: int = Field(default=255, ge=1, le=4096)
    tooltip: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def validate_definition(self) -> StrategyField:
        if self.min_length > self.max_length:
            raise ValueError("min_length cannot exceed max_length")
        if self.type == "select" and not self.options:
            raise ValueError("select fields require options")
        if self.validator == "choice" and not self.options:
            raise ValueError("choice validation requires options")
        if self.pattern:
            re.compile(self.pattern)
        return self

    def validate_value(self, value: str) -> str:
        if self.required and not value:
            raise ValueError("is required")
        if len(value) < self.min_length or len(value) > self.max_length:
            raise ValueError(f"must contain {self.min_length}-{self.max_length} characters")
        if self.pattern and re.fullmatch(self.pattern, value) is None:
            raise ValueError("has an invalid format")
        if self.validator == "choice" and value not in self.options:
            raise ValueError(f"must be one of: {', '.join(self.options)}")
        if self.validator == "nautilus_trader_id":
            try:
                TraderId(value)
            except (TypeError, ValueError) as error:
                raise ValueError("must be a valid Nautilus TraderId") from error
        if self.validator == "nautilus_bar_type":
            try:
                parsed = BarType.from_str(value)
            except (TypeError, ValueError) as error:
                raise ValueError("must be a valid Nautilus BarType") from error
            if str(parsed) != value:
                raise ValueError(f"must use the canonical BarType form: {parsed}")
        return value


class StrategyTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    display_name: str = Field(min_length=1, max_length=80)
    config_map: str = Field(pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
    deployment: str = Field(pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
    log_app: str = Field(pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
    fields: list[StrategyField] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_unique_fields(self) -> StrategyTarget:
        keys = [field.key for field in self.fields]
        if len(keys) != len(set(keys)):
            raise ValueError("strategy field keys must be unique")
        return self

    def validate_values(self, values: dict[str, str]) -> dict[str, str]:
        expected = {field.key for field in self.fields}
        provided = set(values)
        if missing := sorted(expected - provided):
            raise ValueError(f"missing configuration field(s): {', '.join(missing)}")
        if extra := sorted(provided - expected):
            raise ValueError(f"unsupported configuration field(s): {', '.join(extra)}")

        validated: dict[str, str] = {}
        for field in self.fields:
            try:
                validated[field.key] = field.validate_value(values[field.key])
            except ValueError as error:
                raise ValueError(f"{field.key}: {error}") from error
        return validated


class StrategyRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategies: list[StrategyTarget] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_unique_targets(self) -> StrategyRegistry:
        ids = [strategy.id for strategy in self.strategies]
        if len(ids) != len(set(ids)):
            raise ValueError("strategy IDs must be unique")
        config_maps = [strategy.config_map for strategy in self.strategies]
        if len(config_maps) != len(set(config_maps)):
            raise ValueError("strategy ConfigMaps must be unique")
        deployments = [strategy.deployment for strategy in self.strategies]
        if len(deployments) != len(set(deployments)):
            raise ValueError("strategy Deployments must be unique")
        return self

    @classmethod
    def from_path(cls, path: str) -> StrategyRegistry:
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))

    def get(self, strategy_id: str) -> StrategyTarget | None:
        return next((strategy for strategy in self.strategies if strategy.id == strategy_id), None)


class StrategySummary(BaseModel):
    id: str
    display_name: str
    namespace: str
    deployment: str
    log_app: str


class StrategyConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource_version: str = Field(min_length=1)
    values: dict[str, str]


class StrategyConfigResponse(BaseModel):
    strategy_id: str
    display_name: str
    resource_version: str
    values: dict[str, str]
    fields: list[StrategyField]
    restarted_at: str | None = None

    def to_form_json(self) -> str:
        return json.dumps(
            {
                "strategy_id": self.strategy_id,
                "resource_version": self.resource_version,
                "values": self.values,
                "fields": [field.model_dump() for field in self.fields],
            },
            separators=(",", ":"),
        )


class StrategyConfigDashboardResponse(StrategyConfigResponse):
    form_json: str

    @classmethod
    def from_config(cls, config: StrategyConfigResponse) -> StrategyConfigDashboardResponse:
        return cls(**config.model_dump(), form_json=config.to_form_json())


class StrategyStatus(BaseModel):
    strategy_id: str
    display_name: str
    deployment: str
    state: Literal["Ready", "Progressing", "Degraded", "Stopped"]
    state_code: int = Field(ge=0, le=3)
    replicas: str
    desired_replicas: int
    ready_replicas: int
    available_replicas: int
    updated_replicas: int
    generation: int
    observed_generation: int
    image: str
    restarted_at: str | None = None
    restarted_at_unix_ms: int | None = None
    condition: str = ""
