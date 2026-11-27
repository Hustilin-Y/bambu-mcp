from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

from bambu_mcp.store.models import OutboxRow, Plate, PrintRow, Spool, Tariff

_SCHEMA = Path(__file__).with_name("schema.sql")
_SCHEMA_VERSION = 3
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
}
_SEEN_TTL_S = 30 * 24 * 3600
_DEFAULT_TARIFF = (date(2024, 6, 1), 4.32, None)

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
    }
)
_PRINT_FIELDS = frozenset({"spool_id", "grams", "kwh", "cost_filament_uah", "cost_power_uah", "fit"})


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
    ) -> int:
        with self._tx() as conn:
            cur = conn.execute(
                "INSERT INTO spools (brand, material, color_name, color_hex, filament_preset,"
                " price_uah, net_g, remaining_g) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (brand, material, color_name, color_hex, filament_preset, price_uah, net_g, net_g),
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
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO tariffs (valid_from, day_uah, night_uah) VALUES (?, ?, ?) "
                "ON CONFLICT(valid_from) DO UPDATE SET day_uah = excluded.day_uah,"
                " night_uah = excluded.night_uah",
                (valid_from.isoformat(), day_uah, night_uah),
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
        return Tariff(date.fromisoformat(row["valid_from"]), row["day_uah"], row["night_uah"])

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
    ) -> bool:
        """Queue a message. False when one with the same dedupe key is already waiting."""
        with self._tx() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO outbox (kind, text, buttons, snapshot, dedupe, created_at,"
                " next_attempt_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    kind,
                    text,
                    json.dumps(buttons, ensure_ascii=False) if buttons else None,
                    int(snapshot),
                    dedupe,
                    now,
                    now,
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

    def outbox_count(self) -> int:
        return int(self._conn().execute("SELECT COUNT(*) FROM outbox").fetchone()[0])
