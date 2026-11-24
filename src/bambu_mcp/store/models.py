from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

JOB_STATUSES = ("new", "slicing", "sliced", "slice_failed")


@dataclass(frozen=True)
class Spool:
    id: int
    brand: str
    material: str
    color_name: str
    color_hex: str
    filament_preset: str | None
    price_uah: float
    net_g: float
    remaining_g: float
    active: bool
    # For load_spool, copied once by the owner from `slice-agent flatten` output.
    tray_info_idx: str | None = None  # Bambu filament_id of the preset, e.g. "GFA00"
    nozzle_temp_min: int | None = None  # nozzle_temperature_range_low
    nozzle_temp_max: int | None = None  # nozzle_temperature_range_high

    @property
    def price_per_kg_uah(self) -> float:
        return self.price_uah / self.net_g * 1000


@dataclass(frozen=True)
class Plate:
    id: int
    name: str
    bed_type: str
    is_default: bool


@dataclass(frozen=True)
class Tariff:
    valid_from: date
    day_uah: float
    night_uah: float | None


@dataclass(frozen=True)
class PrintRow:
    id: int
    identity: tuple[str, str, str]
    source: str
    started_at: datetime
    ended_at: datetime | None
    outcome: str | None
    actual_min: float | None
    spool_id: int | None
    grams: float | None
    kwh: float | None
    cost_filament_uah: float | None
    cost_power_uah: float | None
    fit: str | None
    deducted_spool_id: int | None = None  # spool the recorded grams were removed from
    deducted_g: float | None = None  # grams actually removed (after the floor at 0)
    job_id: int | None = None  # the sliced job the print's file name matched
    plate: int | None = None  # its plate, None when the printer did not say


@dataclass(frozen=True)
class OutboxRow:
    id: int
    kind: str  # event kind, "auth", "fit", "spool" or "tariff": for logs, never the text
    text: str
    buttons: list[list[dict[str, str]]] | None  # rows of {"id", "text"}
    snapshot: bool  # attach a camera snapshot taken at the first attempt
    dedupe: str
    created_at: float  # epoch seconds
    attempts: int
    next_attempt_at: float


@dataclass(frozen=True)
class Job:
    id: int
    name: str  # the manifest name, unchanged
    kind: str  # "part" or "kit"
    manifest_json: str  # the manifest exactly as received
    file_sha256: str
    created_at: datetime
    status: str  # one of JOB_STATUSES
    settings_json: str | None
    result_json: str | None
    sliced_file: str | None  # a file name, never a path
    error: str | None
