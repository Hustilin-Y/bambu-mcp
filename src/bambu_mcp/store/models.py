from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

JOB_STATUSES = ("new", "slicing", "sliced", "slice_failed")
CONFIRMATION_TTL_S = 600.0  # a print start or stop confirmation is valid 10 minutes


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
    currency: str = "UAH"  # ISO code of price_uah (the name predates currencies)

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
    currency: str = "UAH"  # ISO code of both rates (the names predate currencies)


@dataclass(frozen=True)
class Amortization:
    """The printer's wear per hour of printing, valid from ``valid_from`` on."""

    valid_from: date
    printer_price: float
    life_hours: float  # expected service life in hours of printing
    upkeep_per_hour: float  # nozzles, belts, plates
    currency: str

    @property
    def rate_per_hour(self) -> float:
        return self.printer_price / self.life_hours + self.upkeep_per_hour


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
    cost_amort: float | None = None  # amortization for the actual minutes
    currency: str = "UAH"  # ISO code the row's costs are in


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
    expires_at: float | None = None  # epoch seconds the buttons stop working (a confirmation's)


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
    slot: int | None = None  # the owner's AMS slot choice (1..16), None: chosen by material


@dataclass(frozen=True)
class Confirmation:
    id: str  # 128-bit random hex, the button's callback id
    kind: str  # "start" or "stop"
    job_id: int | None
    plate: int | None
    gate_hash: str | None  # printgate.gate_hash at the time the button was sent
    payload_json: str  # what the press needs to act, opaque to the store
    created_at: float  # epoch seconds
    expires_at: float  # epoch seconds, claims at or after it fail
    state: str  # "pending", "used", "expired" or "cancelled"
    outcome: str | None  # what happened after the press, None until then
