from __future__ import annotations

import contextlib
import json
import math
import re
import secrets
import sqlite3
import threading
import time
from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

from bambu_mcp.store.models import (
    CONFIRMATION_TTL_S,
    JOB_STATUSES,
    Amortization,
    Confirmation,
    Job,
    OutboxRow,
    Plate,
    PrintRow,
    Spool,
    Tariff,
)

_SCHEMA = Path(__file__).with_name("schema.sql")
_CONFIRMATION_ID_RE = re.compile(r"[0-9a-f]{32}", re.ASCII)
_SCHEMA_VERSION = 12
# schema.sql is the version-1 schema; each later version is one migration script.
_MIGRATIONS = {
    2: "ALTER TABLE prints ADD COLUMN deducted_spool_id INTEGER REFERENCES spools(id);\n"
    "ALTER TABLE prints ADD COLUMN deducted_g REAL;",
    # Telegram messages waiting to be delivered, oldest first. A snapshot is a flag:
    # the picture is taken at the first attempt, never stored.
    3: "CREATE TABLE outbox (\n"
    "    id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
    "    kind TEXT NOT NULL,\n"
    "    text TEXT NOT NULL,\n"
    "    buttons TEXT,\n"
    "    snapshot INTEGER NOT NULL DEFAULT 0,\n"
    "    dedupe TEXT NOT NULL UNIQUE,\n"
    "    created_at REAL NOT NULL,\n"
    "    attempts INTEGER NOT NULL DEFAULT 0,\n"
    "    next_attempt_at REAL NOT NULL\n"
    ");",
    # Print jobs from CAD Workshop manifests. Their files live in data_dir/jobs/<id>/.
    4: "CREATE TABLE jobs (\n"
    "    id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
    "    name TEXT NOT NULL,\n"
    "    kind TEXT NOT NULL CHECK (kind IN ('part', 'kit')),\n"
    "    manifest_json TEXT NOT NULL,\n"
    "    file_sha256 TEXT NOT NULL,\n"
    "    created_at TEXT NOT NULL,\n"
    "    status TEXT NOT NULL DEFAULT 'new'"
    " CHECK (status IN ('new', 'slicing', 'sliced', 'slice_failed')),\n"
    "    settings_json TEXT,\n"
    "    result_json TEXT,\n"
    "    sliced_file TEXT,\n"
    "    error TEXT\n"
    ");\n"
    "CREATE INDEX jobs_name ON jobs (name);",
    # The sliced job a print was matched to by its file name, and the plate printed.
    5: "ALTER TABLE prints ADD COLUMN job_id INTEGER REFERENCES jobs(id);\n"
    "ALTER TABLE prints ADD COLUMN plate INTEGER;",
    # What load_spool sends to the AMS: the preset's filament_id and nozzle range.
    6: "ALTER TABLE spools ADD COLUMN tray_info_idx TEXT;\n"
    "ALTER TABLE spools ADD COLUMN nozzle_temp_min INTEGER;\n"
    "ALTER TABLE spools ADD COLUMN nozzle_temp_max INTEGER;",
    # The AMS slot the owner chose for a job (1..16 as the owner counts), NULL: by material.
    7: "ALTER TABLE jobs ADD COLUMN slot INTEGER CHECK (slot BETWEEN 1 AND 16);",
    # Telegram buttons that start or stop a print. Single use: a press claims the row.
    8: "CREATE TABLE confirmations (\n"
    "    id TEXT PRIMARY KEY,\n"
    "    kind TEXT NOT NULL CHECK (kind IN ('start', 'stop')),\n"
    "    job_id INTEGER REFERENCES jobs(id),\n"
    "    plate INTEGER,\n"
    "    gate_hash TEXT,\n"
    "    payload_json TEXT NOT NULL,\n"
    "    created_at REAL NOT NULL,\n"
    "    expires_at REAL NOT NULL,\n"
    "    state TEXT NOT NULL DEFAULT 'pending'"
    " CHECK (state IN ('pending', 'used', 'expired', 'cancelled')),\n"
    "    outcome TEXT\n"
    ");",
    # Money: every price, rate and journal row keeps its ISO currency, nothing is converted.
    # The printer's amortization has a history by valid_from, like the tariff.
    9: "ALTER TABLE spools ADD COLUMN currency TEXT NOT NULL DEFAULT 'UAH';\n"
    "ALTER TABLE tariffs ADD COLUMN currency TEXT NOT NULL DEFAULT 'UAH';\n"
    "ALTER TABLE prints ADD COLUMN currency TEXT NOT NULL DEFAULT 'UAH';\n"
    "ALTER TABLE prints ADD COLUMN cost_amort REAL;\n"
    "CREATE TABLE amortization (\n"
    "    valid_from TEXT PRIMARY KEY,\n"
    "    printer_price REAL NOT NULL CHECK (printer_price >= 0),\n"
    "    life_hours REAL NOT NULL CHECK (life_hours > 0),\n"
    "    upkeep_per_hour REAL NOT NULL DEFAULT 0 CHECK (upkeep_per_hour >= 0),\n"
    "    currency TEXT NOT NULL\n"
    ");\n"
    "INSERT OR IGNORE INTO meta (key, value) VALUES"
    " ('currency', 'UAH'), ('currency_symbol', 'грн'), ('country', 'UA');",
    # When a message's buttons stop working (a start or stop confirmation's expiry), NULL: never.
    10: "ALTER TABLE outbox ADD COLUMN expires_at REAL;",
    # The owner's print goal, the chosen evaluation variant (a settings layer) and the
    # evaluated variants, all JSON. NULL: none yet.
    11: "ALTER TABLE jobs ADD COLUMN goal_json TEXT;\n"
    "ALTER TABLE jobs ADD COLUMN variant_json TEXT;\n"
    "ALTER TABLE jobs ADD COLUMN variants_json TEXT;",
    # The owner's explicit settings (their words, ruling R2), kept as their own layer. NULL: none.
    12: "ALTER TABLE jobs ADD COLUMN explicit_json TEXT;",
}
_SEEN_TTL_S = 30 * 24 * 3600
_DEFAULT_TARIFF = (date(2024, 6, 1), 4.32, None)
DEFAULT_CURRENCY = ("UAH", "грн")
DEFAULT_COUNTRY = "UA"

_SPOOL_FIELDS = frozenset(
    {
        "brand",
        "material",
        "color_name",
        "color_hex",
        "filament_preset",
        "price_uah",
        "net_g",
        "remaining_g",
        "active",
        "tray_info_idx",
        "nozzle_temp_min",
        "nozzle_temp_max",
    }
)
_PRINT_FIELDS = frozenset(
    {
        "spool_id",
        "grams",
        "kwh",
        "cost_filament_uah",
        "cost_power_uah",
        "cost_amort",
        "currency",
        "fit",
        "source",
        "job_id",
        "plate",
    }
)
_JOB_FIELDS = frozenset({
    "status", "settings_json", "result_json", "sliced_file", "error", "slot",
    "goal_json", "variant_json", "variants_json", "explicit_json",
})


def _norm(identity: tuple) -> tuple[str, str, str]:
    name, task_id, start = (part or "" for part in identity)
    return name, task_id, start


def _spool(row: sqlite3.Row) -> Spool:
    return Spool(
        id=row["id"],
        brand=row["brand"],
        material=row["material"],
        color_name=row["color_name"],
        color_hex=row["color_hex"],
        filament_preset=row["filament_preset"],
        price_uah=row["price_uah"],
        net_g=row["net_g"],
        remaining_g=row["remaining_g"],
        active=bool(row["active"]),
        tray_info_idx=row["tray_info_idx"],
        nozzle_temp_min=row["nozzle_temp_min"],
        nozzle_temp_max=row["nozzle_temp_max"],
        currency=row["currency"],
    )


def _tariff(row: sqlite3.Row) -> Tariff:
    return Tariff(
        date.fromisoformat(row["valid_from"]), row["day_uah"], row["night_uah"], row["currency"]
    )


def _amortization(row: sqlite3.Row) -> Amortization:
    return Amortization(
        valid_from=date.fromisoformat(row["valid_from"]),
        printer_price=row["printer_price"],
        life_hours=row["life_hours"],
        upkeep_per_hour=row["upkeep_per_hour"],
        currency=row["currency"],
    )


def _print(row: sqlite3.Row) -> PrintRow:
    ended = row["ended_at"]
    return PrintRow(
        id=row["id"],
        identity=(row["name"], row["task_id"], row["gcode_start"]),
        source=row["source"],
        started_at=datetime.fromisoformat(row["started_at"]),
        ended_at=datetime.fromisoformat(ended) if ended else None,
        outcome=row["outcome"],
        actual_min=row["actual_min"],
        spool_id=row["spool_id"],
        grams=row["grams"],
        kwh=row["kwh"],
        cost_filament_uah=row["cost_filament_uah"],
        cost_power_uah=row["cost_power_uah"],
        fit=row["fit"],
        deducted_spool_id=row["deducted_spool_id"],
        deducted_g=row["deducted_g"],
        job_id=row["job_id"],
        plate=row["plate"],
        cost_amort=row["cost_amort"],
        currency=row["currency"],
    )


def _confirmation(row: sqlite3.Row) -> Confirmation:
    return Confirmation(
        id=row["id"],
        kind=row["kind"],
        job_id=row["job_id"],
        plate=row["plate"],
        gate_hash=row["gate_hash"],
        payload_json=row["payload_json"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        state=row["state"],
        outcome=row["outcome"],
    )


def _job(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        name=row["name"],
        kind=row["kind"],
        manifest_json=row["manifest_json"],
        file_sha256=row["file_sha256"],
        created_at=datetime.fromisoformat(row["created_at"]),
        status=row["status"],
        settings_json=row["settings_json"],
        result_json=row["result_json"],
        sliced_file=row["sliced_file"],
        error=row["error"],
        slot=row["slot"],
        goal_json=row["goal_json"],
        variant_json=row["variant_json"],
        variants_json=row["variants_json"],
        explicit_json=row["explicit_json"],
    )


class Store:
    """SQLite store. One connection per thread (asyncio loop + paho thread), WAL mode."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._conns: list[sqlite3.Connection] = []
        self._lock = threading.Lock()

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            # Each connection is only used by its own thread; close() runs from another one.
            conn = sqlite3.connect(self._path, timeout=10, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
            with self._lock:
                self._conns.append(conn)
        return conn

    @contextlib.contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """A write transaction, or a savepoint inside the thread's open ``transaction()``."""
        conn = self._conn()
        depth = getattr(self._local, "depth", 0)
        if depth == 0:
            with conn:  # commits, or rolls back on an exception
                if not conn.in_transaction:
                    conn.execute("BEGIN")
                self._local.depth = 1
                try:
                    yield conn
                finally:
                    self._local.depth = 0
            return
        name = f"sp{depth}"
        conn.execute(f"SAVEPOINT {name}")
        self._local.depth = depth + 1
        try:
            yield conn
        except BaseException:
            conn.execute(f"ROLLBACK TO {name}")
            conn.execute(f"RELEASE {name}")
            raise
        else:
            conn.execute(f"RELEASE {name}")
        finally:
            self._local.depth = depth

    def transaction(self) -> contextlib.AbstractContextManager[sqlite3.Connection]:
        """Group several store calls into one atomic write. Nested calls are savepoints.

        Synchronous code only: never hold it across an ``await``, since other tasks on
        the same thread share the connection.
        """
        return self._tx()

    def close(self) -> None:
        """Close every connection. Call when no other thread is using the store."""
        with self._lock:
            conns, self._conns = self._conns, []
        for conn in conns:
            conn.close()
        self._local = threading.local()

    # -- schema ---------------------------------------------------------

    def migrate(self) -> None:
        conn = self._conn()
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version < 1:
            conn.executescript(
                "BEGIN;\n" + _SCHEMA.read_text(encoding="utf-8") + "\nPRAGMA user_version = 1;\nCOMMIT;"
            )
            version = 1
        for target in range(version + 1, _SCHEMA_VERSION + 1):
            conn.executescript(
                f"BEGIN;\n{_MIGRATIONS[target]}\nPRAGMA user_version = {target};\nCOMMIT;"
            )
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO tariffs (valid_from, day_uah, night_uah) VALUES (?, ?, ?)",
                (_DEFAULT_TARIFF[0].isoformat(), _DEFAULT_TARIFF[1], _DEFAULT_TARIFF[2]),
            )
            conn.execute("DELETE FROM seen WHERE seen_at < ?", (time.time() - _SEEN_TTL_S,))

    # -- dedupe keys (SeenStore) ------------------------------------------

    def seen_contains(self, key: str) -> bool:
        row = self._conn().execute("SELECT 1 FROM seen WHERE key = ?", (key,)).fetchone()
        return row is not None

    def seen_add(self, key: str) -> None:
        with self._tx() as conn:
            conn.execute("INSERT OR IGNORE INTO seen (key, seen_at) VALUES (?, ?)", (key, time.time()))

    # -- meta ------------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        row = self._conn().execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # -- currency and country --------------------------------------------

    def currency(self) -> tuple[str, str]:
        """The current ISO currency code and its symbol. New prices and rates are in it."""
        return (
            self.get_meta("currency") or DEFAULT_CURRENCY[0],
            self.get_meta("currency_symbol") or DEFAULT_CURRENCY[1],
        )

    def set_currency(self, code: str, symbol: str) -> None:
        with self._tx():
            self.set_meta("currency", code)
            self.set_meta("currency_symbol", symbol)

    def country(self) -> str:
        """ISO country code: the default timezone and whether the tariff reminder runs."""
        return self.get_meta("country") or DEFAULT_COUNTRY

    def set_country(self, code: str) -> None:
        self.set_meta("country", code)

    # -- spools ----------------------------------------------------------

    def add_spool(
        self,
        brand: str,
        material: str,
        color_name: str,
        color_hex: str,
        filament_preset: str | None,
        price_uah: float,
        net_g: float,
        *,
        tray_info_idx: str | None = None,
        nozzle_temp_min: int | None = None,
        nozzle_temp_max: int | None = None,
    ) -> int:
        with self._tx() as conn:
            cur = conn.execute(
                "INSERT INTO spools (brand, material, color_name, color_hex, filament_preset,"
                " price_uah, net_g, remaining_g, tray_info_idx, nozzle_temp_min, nozzle_temp_max,"
                " currency) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    brand,
                    material,
                    color_name,
                    color_hex,
                    filament_preset,
                    price_uah,
                    net_g,
                    net_g,
                    tray_info_idx,
                    nozzle_temp_min,
                    nozzle_temp_max,
                    self.currency()[0],
                ),
            )
            return int(cur.lastrowid)

    def update_spool(self, id: int, **fields: Any) -> None:
        unknown = set(fields) - _SPOOL_FIELDS
        if unknown:
            raise ValueError(f"unknown spool fields: {sorted(unknown)}")
        if not fields:
            return
        if "active" in fields:
            fields["active"] = int(bool(fields["active"]))
        if "price_uah" in fields:  # a new price is in the current currency
            fields["currency"] = self.currency()[0]
        assignments = ", ".join(f"{name} = ?" for name in fields)
        with self._tx() as conn:
            cur = conn.execute(f"UPDATE spools SET {assignments} WHERE id = ?", (*fields.values(), id))
            if cur.rowcount == 0:
                raise KeyError(f"no spool {id}")

    def spools(self, active_only: bool = True) -> list[Spool]:
        sql = "SELECT * FROM spools" + (" WHERE active = 1" if active_only else "") + " ORDER BY id"
        return [_spool(r) for r in self._conn().execute(sql)]

    def adjust_spool(self, id: int, used_g: float) -> None:
        """Subtract used grams from the remaining weight (floored at 0)."""
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE spools SET remaining_g = MAX(remaining_g - ?, 0) WHERE id = ?", (used_g, id)
            )
            if cur.rowcount == 0:
                raise KeyError(f"no spool {id}")

    def set_spool_weight(self, id: int, remaining_g: float) -> None:
        self.update_spool(id, remaining_g=remaining_g)

    # -- AMS slots -------------------------------------------------------

    def map_slot(self, ams: int, slot: int, spool_id: int | None) -> None:
        """Map an AMS slot to a spool, replacing any previous mapping. None clears the slot."""
        with self._tx() as conn:
            if spool_id is None:
                conn.execute("DELETE FROM slots WHERE ams = ? AND slot = ?", (ams, slot))
            else:
                conn.execute(
                    "INSERT INTO slots (ams, slot, spool_id) VALUES (?, ?, ?) "
                    "ON CONFLICT(ams, slot) DO UPDATE SET spool_id = excluded.spool_id",
                    (ams, slot, spool_id),
                )

    def slot_spool(self, ams: int, slot: int) -> Spool | None:
        row = (
            self._conn()
            .execute(
                "SELECT s.* FROM slots m JOIN spools s ON s.id = m.spool_id"
                " WHERE m.ams = ? AND m.slot = ?",
                (ams, slot),
            )
            .fetchone()
        )
        return _spool(row) if row else None

    # -- plates ----------------------------------------------------------

    def add_plate(self, name: str, bed_type: str) -> int:
        with self._tx() as conn:
            cur = conn.execute("INSERT INTO plates (name, bed_type) VALUES (?, ?)", (name, bed_type))
            return int(cur.lastrowid)

    def plates(self) -> list[Plate]:
        rows = self._conn().execute("SELECT * FROM plates ORDER BY id")
        return [Plate(r["id"], r["name"], r["bed_type"], bool(r["is_default"])) for r in rows]

    def set_default_plate(self, id: int) -> None:
        with self._tx() as conn:
            if conn.execute("SELECT 1 FROM plates WHERE id = ?", (id,)).fetchone() is None:
                raise KeyError(f"no plate {id}")
            conn.execute("UPDATE plates SET is_default = (id = ?)", (id,))

    # -- tariffs ---------------------------------------------------------

    def add_tariff(self, valid_from: date, day_uah: float, night_uah: float | None) -> None:
        """Rates in the current currency."""
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO tariffs (valid_from, day_uah, night_uah, currency) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(valid_from) DO UPDATE SET day_uah = excluded.day_uah,"
                " night_uah = excluded.night_uah, currency = excluded.currency",
                (valid_from.isoformat(), day_uah, night_uah, self.currency()[0]),
            )

    def tariff_on(self, d: date) -> Tariff:
        row = (
            self._conn()
            .execute(
                "SELECT * FROM tariffs WHERE valid_from <= ? ORDER BY valid_from DESC LIMIT 1",
                (d.isoformat(),),
            )
            .fetchone()
        )
        if row is None:
            raise LookupError(f"no tariff valid on {d.isoformat()}")
        return _tariff(row)

    # -- amortization ----------------------------------------------------

    def add_amortization(
        self,
        valid_from: date,
        printer_price: float,
        life_hours: float,
        upkeep_per_hour: float = 0.0,
    ) -> None:
        """The printer's amortization from ``valid_from`` on, in the current currency."""
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO amortization (valid_from, printer_price, life_hours, upkeep_per_hour,"
                " currency) VALUES (?, ?, ?, ?, ?) ON CONFLICT(valid_from) DO UPDATE SET"
                " printer_price = excluded.printer_price, life_hours = excluded.life_hours,"
                " upkeep_per_hour = excluded.upkeep_per_hour, currency = excluded.currency",
                (valid_from.isoformat(), printer_price, life_hours, upkeep_per_hour, self.currency()[0]),
            )

    def amortization_on(self, d: date) -> Amortization | None:
        """The entry valid on ``d``, None when none is set yet (no amortization line)."""
        row = (
            self._conn()
            .execute(
                "SELECT * FROM amortization WHERE valid_from <= ? ORDER BY valid_from DESC LIMIT 1",
                (d.isoformat(),),
            )
            .fetchone()
        )
        return _amortization(row) if row else None

    # -- print journal ---------------------------------------------------

    def open_print(
        self, identity: tuple, started_at: datetime, source: Literal["external", "job"]
    ) -> int:
        name, task_id, start = _norm(identity)
        with self._tx() as conn:
            cur = conn.execute(
                "INSERT INTO prints (name, task_id, gcode_start, source, started_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (name, task_id, start, source, started_at.isoformat()),
            )
            return int(cur.lastrowid)

    def close_print(
        self, id: int, outcome: str, ended_at: datetime, actual_min: float | None
    ) -> None:
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE prints SET outcome = ?, ended_at = ?, actual_min = ? WHERE id = ?",
                (outcome, ended_at.isoformat(), actual_min, id),
            )
            if cur.rowcount == 0:
                raise KeyError(f"no print {id}")

    def set_print_fields(self, id: int, **fields: Any) -> None:
        unknown = set(fields) - _PRINT_FIELDS
        if unknown:
            raise ValueError(f"unknown print fields: {sorted(unknown)}")
        if not fields:
            return
        assignments = ", ".join(f"{name} = ?" for name in fields)
        with self._tx() as conn:
            cur = conn.execute(f"UPDATE prints SET {assignments} WHERE id = ?", (*fields.values(), id))
            if cur.rowcount == 0:
                raise KeyError(f"no print {id}")

    def print_by_identity(self, identity: tuple) -> PrintRow | None:
        """Latest journal row for this identity (None and "" are equal)."""
        row = (
            self._conn()
            .execute(
                "SELECT * FROM prints WHERE name = ? AND task_id = ? AND gcode_start = ?"
                " ORDER BY id DESC LIMIT 1",
                _norm(identity),
            )
            .fetchone()
        )
        return _print(row) if row else None

    def rebook_print_grams(
        self,
        id: int,
        spool_id: int | None,
        grams: float | None,
        cost_filament_uah: float | None,
    ) -> None:
        """Set a print's spool and grams and move its spool deduction, in one transaction.

        The grams removed earlier (``deducted_g``, after the floor at 0) go back to
        the spool they were removed from (``deducted_spool_id``). Then ``grams`` are
        removed from ``spool_id`` (at most its remaining weight) and recorded.
        """
        with self._tx() as conn:
            row = conn.execute(
                "SELECT deducted_spool_id, deducted_g FROM prints WHERE id = ?", (id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"no print {id}")
            if row["deducted_spool_id"] is not None and row["deducted_g"]:
                conn.execute(
                    "UPDATE spools SET remaining_g = remaining_g + ? WHERE id = ?",
                    (row["deducted_g"], row["deducted_spool_id"]),
                )
            deducted_spool, deducted = None, None
            if spool_id is not None:
                spool = conn.execute(
                    "SELECT remaining_g FROM spools WHERE id = ?", (spool_id,)
                ).fetchone()
                if spool is None:
                    raise KeyError(f"no spool {spool_id}")  # rolls the refund back
                if grams:
                    deducted_spool = spool_id
                    deducted = min(grams, max(spool["remaining_g"], 0.0))
                    conn.execute(
                        "UPDATE spools SET remaining_g = remaining_g - ? WHERE id = ?",
                        (deducted, spool_id),
                    )
            conn.execute(
                "UPDATE prints SET spool_id = ?, grams = ?, cost_filament_uah = ?,"
                " deducted_spool_id = ?, deducted_g = ? WHERE id = ?",
                (spool_id, grams, cost_filament_uah, deducted_spool, deducted, id),
            )

    def open_prints(self) -> list[PrintRow]:
        """Rows without an end, oldest first."""
        rows = self._conn().execute("SELECT * FROM prints WHERE ended_at IS NULL ORDER BY id")
        return [_print(r) for r in rows]

    def print_by_id(self, id: int) -> PrintRow | None:
        row = self._conn().execute("SELECT * FROM prints WHERE id = ?", (id,)).fetchone()
        return _print(row) if row else None

    def history(self, part: str | None, limit: int) -> list[PrintRow]:
        """Newest first. `part` is a case-insensitive substring of the job name."""
        sql = "SELECT * FROM prints"
        args: list[Any] = []
        if part:
            escaped = part.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            sql += " WHERE name LIKE ? ESCAPE '\\'"
            args.append(f"%{escaped}%")
        sql += " ORDER BY started_at DESC, id DESC LIMIT ?"
        args.append(limit)
        return [_print(r) for r in self._conn().execute(sql, args)]

    # -- outbox ----------------------------------------------------------

    def outbox_add(
        self,
        kind: str,
        text: str,
        dedupe: str,
        *,
        buttons: list[list[dict[str, str]]] | None = None,
        snapshot: bool = False,
        now: float,
        expires_at: float | None = None,
    ) -> bool:
        """Queue a message. False when one with the same dedupe key is already waiting.

        ``expires_at`` (epoch seconds): when its buttons stop working, None for the add-in's default.
        """
        with self._tx() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO outbox (kind, text, buttons, snapshot, dedupe, created_at,"
                " next_attempt_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    kind,
                    text,
                    json.dumps(buttons, ensure_ascii=False) if buttons else None,
                    int(snapshot),
                    dedupe,
                    now,
                    now,
                    expires_at,
                ),
            )
            return cur.rowcount == 1

    def outbox_head(self) -> OutboxRow | None:
        """The oldest waiting message: delivery is strictly in order."""
        row = self._conn().execute("SELECT * FROM outbox ORDER BY id LIMIT 1").fetchone()
        if row is None:
            return None
        return OutboxRow(
            id=row["id"],
            kind=row["kind"],
            text=row["text"],
            buttons=json.loads(row["buttons"]) if row["buttons"] else None,
            snapshot=bool(row["snapshot"]),
            dedupe=row["dedupe"],
            created_at=row["created_at"],
            attempts=row["attempts"],
            next_attempt_at=row["next_attempt_at"],
            expires_at=row["expires_at"],
        )

    def outbox_defer(self, id: int, attempts: int, next_attempt_at: float) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE outbox SET attempts = ?, next_attempt_at = ? WHERE id = ?",
                (attempts, next_attempt_at, id),
            )

    def outbox_delete(self, id: int) -> None:
        with self._tx() as conn:
            conn.execute("DELETE FROM outbox WHERE id = ?", (id,))

    def outbox_dedupes(self) -> set[str]:
        """Dedupe keys of every waiting message."""
        return {r["dedupe"] for r in self._conn().execute("SELECT dedupe FROM outbox")}

    def outbox_count(self) -> int:
        return int(self._conn().execute("SELECT COUNT(*) FROM outbox").fetchone()[0])

    # -- jobs ------------------------------------------------------------

    def add_job(
        self, name: str, kind: str, manifest_json: str, file_sha256: str, created_at: datetime
    ) -> int:
        """A new job in status ``new``. Its files are handled by ``bambu_mcp.jobs``."""
        with self._tx() as conn:
            cur = conn.execute(
                "INSERT INTO jobs (name, kind, manifest_json, file_sha256, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (name, kind, manifest_json, file_sha256, created_at.isoformat()),
            )
            return int(cur.lastrowid)

    def job(self, id: int) -> Job | None:
        row = self._conn().execute("SELECT * FROM jobs WHERE id = ?", (id,)).fetchone()
        return _job(row) if row else None

    def jobs(self, limit: int) -> list[Job]:
        """Newest first."""
        rows = self._conn().execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,))
        return [_job(r) for r in rows]

    def jobs_with_status(self, status: str) -> list[Job]:
        """Oldest first."""
        rows = self._conn().execute("SELECT * FROM jobs WHERE status = ? ORDER BY id", (status,))
        return [_job(r) for r in rows]

    @staticmethod
    def _job_assignments(fields: dict[str, Any]) -> str:
        unknown = set(fields) - _JOB_FIELDS
        if unknown:
            raise ValueError(f"unknown job fields: {sorted(unknown)}")
        if "status" in fields and fields["status"] not in JOB_STATUSES:
            raise ValueError(f"job status must be one of {', '.join(JOB_STATUSES)}")
        return ", ".join(f"{name} = ?" for name in fields)

    def update_job(self, id: int, **fields: Any) -> None:
        assignments = self._job_assignments(fields)
        if not fields:
            return
        with self._tx() as conn:
            cur = conn.execute(f"UPDATE jobs SET {assignments} WHERE id = ?", (*fields.values(), id))
            if cur.rowcount == 0:
                raise KeyError(f"no job {id}")

    def transition_job(self, id: int, from_statuses: tuple[str, ...], **fields: Any) -> bool:
        """Update the job only while its status is one of ``from_statuses``, in one statement.

        False when the job is in another status (or does not exist): nothing changed.
        """
        if not fields or not from_statuses:
            raise ValueError("transition_job needs fields and from_statuses")
        assignments = self._job_assignments(fields)
        marks = ", ".join("?" for _ in from_statuses)
        with self._tx() as conn:
            cur = conn.execute(
                f"UPDATE jobs SET {assignments} WHERE id = ? AND status IN ({marks})",
                (*fields.values(), id, *from_statuses),
            )
            return cur.rowcount == 1

    # -- confirmations ---------------------------------------------------

    def add_confirmation(
        self,
        id: str | None,
        kind: str,
        *,
        job_id: int | None,
        plate: int | None,
        gate_hash: str | None,
        payload_json: str,
        now: float,
        ttl_s: float = CONFIRMATION_TTL_S,
    ) -> Confirmation:
        """A new ``pending`` confirmation, valid from ``now`` for ``ttl_s`` seconds.

        ``id`` None makes a random 128-bit one (``printgate.new_confirmation_id``), a given
        one must be 32 lower-case hex characters. ``ttl_s`` is over 0 and at most 10 minutes.
        """
        if id is None:
            id = secrets.token_hex(16)
        elif not (isinstance(id, str) and _CONFIRMATION_ID_RE.fullmatch(id)):
            raise ValueError("a confirmation id is 32 lower-case hex characters")
        if not (isinstance(ttl_s, int | float) and math.isfinite(ttl_s) and 0 < ttl_s <= CONFIRMATION_TTL_S):
            raise ValueError(f"ttl_s must be over 0 and at most {CONFIRMATION_TTL_S:g} seconds")
        with self._tx() as conn:
            row = conn.execute(
                "INSERT INTO confirmations (id, kind, job_id, plate, gate_hash, payload_json,"
                " created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING *",
                (id, kind, job_id, plate, gate_hash, payload_json, now, now + ttl_s),
            ).fetchone()
            return _confirmation(row)

    def confirmation(self, id: str) -> Confirmation | None:
        row = self._conn().execute("SELECT * FROM confirmations WHERE id = ?", (id,)).fetchone()
        return _confirmation(row) if row else None

    def claim_confirmation(self, id: str, now: float) -> Confirmation | None:
        """Move a pending, unexpired confirmation to ``used`` and return it, in one statement.

        At most one caller ever gets it, also across connections. A pending one past
        its expiry is marked ``expired``. None for expired, used, cancelled or unknown.
        """
        with self._tx() as conn:
            row = conn.execute(
                "UPDATE confirmations SET state = 'used'"
                " WHERE id = ? AND state = 'pending' AND expires_at > ? RETURNING *",
                (id, now),
            ).fetchone()
            if row is not None:
                return _confirmation(row)
            conn.execute(
                "UPDATE confirmations SET state = 'expired'"
                " WHERE id = ? AND state = 'pending' AND expires_at <= ?",
                (id, now),
            )
            return None

    def set_confirmation_outcome(self, id: str, outcome: str) -> None:
        """What a press led to: ``started``, ``unconfirmed``, ``refused:<reason>`` or ``cancelled``."""
        with self._tx() as conn:
            cur = conn.execute("UPDATE confirmations SET outcome = ? WHERE id = ?", (outcome, id))
            if cur.rowcount == 0:
                raise KeyError("no such confirmation")  # the id stays out of logs and errors

    def undecided_confirmations(self, kind: str) -> list[Confirmation]:
        """``used`` confirmations of ``kind`` with no outcome: a press a restart cut off. Oldest first."""
        rows = self._conn().execute(
            "SELECT * FROM confirmations WHERE kind = ? AND state = 'used' AND outcome IS NULL"
            " ORDER BY created_at",
            (kind,),
        )
        return [_confirmation(r) for r in rows]

    def expire_pending_confirmations(self, job_id: int, kind: str, outcome: str) -> int:
        """Expire the job's pending confirmations of ``kind`` now, with ``outcome`` saying why.

        Returns how many. A press on one is then answered as spent.
        """
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE confirmations SET state = 'expired', outcome = ?"
                " WHERE job_id = ? AND kind = ? AND state = 'pending'",
                (outcome, job_id, kind),
            )
            return cur.rowcount

    def cancel_confirmation(self, id: str) -> bool:
        """Cancel a pending confirmation. False when it is not pending (or unknown)."""
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE confirmations SET state = 'cancelled' WHERE id = ? AND state = 'pending'",
                (id,),
            )
            return cur.rowcount == 1
