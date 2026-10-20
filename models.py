"""Pydantic models that enforce the DocK P5 contract."""
from datetime import datetime, timezone
from typing import Literal, Optional, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

VitalType = Literal[
    "heart_rate", "spo2", "blood_pressure", "temperature", "weight", "height",
    "gsr",  # EXTENSION, not in P4's original list: skin-sensor output voltage in millivolts (raw, uncalibrated)
]

UNITS = {
    "gsr": "mV",
    "heart_rate": "bpm",
    "spo2": "%",
    "blood_pressure": "mmHg",
    "temperature": "C",
    "weight": "kg",
    "height": "cm",
}


# Name of the physical sensor behind each type (shown as "sensor" in /vitals).
SENSORS = {
    "heart_rate": "AD8232 ECG",
    "spo2": "MAX30102",
    "temperature": "MLX90614",
    "weight": "HX711 load cells",
    "gsr": "GSR Skin Current Sensor V2.0",
}


# Fixed order and names for the simple /data output.
SENSOR_INFO = [
    {"type": "spo2", "name": "SpO2", "model": "MAX30102"},
    {"type": "temperature", "name": "Temperature", "model": "MLX90614"},
    {"type": "heart_rate", "name": "Heart Rate", "model": "AD8232"},
    {"type": "gsr", "name": "GSR", "model": "GSR V2.0"},
    {"type": "weight", "name": "Weight", "model": "HX711"},
]


class BloodPressureValue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    systolic: int
    diastolic: int


class Reading(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: VitalType
    value: Union[int, float, BloodPressureValue]
    unit: str
    confidence: float = Field(ge=0.0, le=1.0)
    sensor: Optional[str] = None
    measured_at: datetime

    @field_validator("measured_at")
    @classmethod
    def _aware_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("measured_at must be timezone-aware")
        return v.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _check_shape(self) -> "Reading":
        if self.unit != UNITS[self.type]:
            raise ValueError(f"unit for {self.type} must be {UNITS[self.type]!r}")
        is_bp = isinstance(self.value, BloodPressureValue)
        if (self.type == "blood_pressure") != is_bp:
            raise ValueError("blood_pressure is the only type with an object value")
        return self

    @field_serializer("measured_at")
    def _ser_time(self, v: datetime) -> str:
        return v.strftime("%Y-%m-%dT%H:%M:%SZ")


class VitalsResponse(BaseModel):
    measuring: Optional[VitalType] = None
    readings: list[Reading]


class ErrorBody(BaseModel):
    error: str
    message: str
