CREATE TABLE seen (
    key TEXT PRIMARY KEY,
    seen_at REAL NOT NULL
);

CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE spools (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand TEXT NOT NULL,
    material TEXT NOT NULL,
    color_name TEXT NOT NULL,
    color_hex TEXT NOT NULL,
    filament_preset TEXT,
    price_uah REAL NOT NULL,
    net_g REAL NOT NULL,
    remaining_g REAL NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE slots (
    ams INTEGER NOT NULL,
    slot INTEGER NOT NULL,
    spool_id INTEGER NOT NULL REFERENCES spools(id),
    PRIMARY KEY (ams, slot)
);

CREATE TABLE plates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    bed_type TEXT NOT NULL,
    is_default INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE tariffs (
    valid_from TEXT PRIMARY KEY,
    day_uah REAL NOT NULL,
    night_uah REAL
);

-- Identity columns hold "" (never NULL) so None and "" are the same identity.
CREATE TABLE prints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL DEFAULT '',
    task_id TEXT NOT NULL DEFAULT '',
    gcode_start TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    outcome TEXT,
    actual_min REAL,
    spool_id INTEGER REFERENCES spools(id),
    grams REAL,
    kwh REAL,
    cost_filament_uah REAL,
    cost_power_uah REAL,
    fit TEXT
);
CREATE INDEX prints_identity ON prints (name, task_id, gcode_start);
CREATE INDEX prints_started ON prints (started_at);
