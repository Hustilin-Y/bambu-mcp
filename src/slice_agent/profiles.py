"""Flatten Bambu Studio presets into the self-contained JSON files the Studio CLI needs.

Recipe verified in phase 0 (docs/phase0-report.md, "CLI recipe"): resolve `inherits`, then
`include` templates, then own keys. Mark the result as a user preset that inherits the system
preset of the same name, set the *_settings_id keys, drop machine keys from process and filament.
Generated files live outside the repo (Bambu presets are AGPL data).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from bambu_profiles import PrinterProfile
from slice_agent.config import AgentConfig

KINDS = ("machine", "process", "filament")
# Keys of a template that must not leak into the preset including it.
_TEMPLATE_SKIP = {"name", "type", "from", "instantiation", "setting_id"}
# Machine keys that stay in process and filament (identity, not machine settings).
_MACHINE_KEEP = {"name", "inherits", "compatible_printers", "from", "type", "setting_id", "instantiation"}
_ID_KEY = {"machine": "printer_settings_id", "process": "print_settings_id", "filament": "filament_settings_id"}

Index = dict[str, dict[str, Path]]


def preset_roots(cfg: AgentConfig) -> list[Path]:
    """Preset roots in lookup order: Studio system presets, user presets, install resources."""
    data = cfg.studio_data
    return [data / "system" / "BBL", *sorted((data / "user").glob("*")), cfg.studio_resources]


def _index(roots: list[Path]) -> Index:
    idx: Index = {k: {} for k in KINDS}
    for kind in KINDS:
        for root in roots:
            for f in sorted((root / kind).glob("*.json")):
                try:
                    name = json.loads(f.read_text(encoding="utf-8")).get("name", f.stem)
                except (ValueError, UnicodeDecodeError):
                    continue
                idx[kind].setdefault(name, f)  # earlier roots win
    return idx


def _resolve(kind: str, name: str, idx: Index, seen: tuple[str, ...] = ()) -> dict:
    if name in seen:
        raise ValueError(f"inherits loop: {' -> '.join((*seen, name))}")
    try:
        path = idx[kind][name]
    except KeyError:
        raise KeyError(f"{kind} preset not found: {name!r}") from None
    data = json.loads(path.read_text(encoding="utf-8"))
    parent = data.get("inherits")
    merged = _resolve(kind, parent, idx, (*seen, name)) if parent else {}
    for inc in data.get("include", []):  # order: parent < included templates < own keys
        tpl = _resolve(kind, inc, idx, (*seen, name))
        merged.update({k: v for k, v in tpl.items() if k not in _TEMPLATE_SKIP})
    merged.update(data)
    merged.pop("inherits", None)
    merged.pop("include", None)
    return merged


def resolve_preset(kind: str, name: str, roots: list[Path]) -> dict:
    return _resolve(kind, name, _index(roots))


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _finish(kind: str, name: str, data: dict, machine_keys: set[str]) -> dict:
    data["inherits"] = name  # the CLI identifies the system preset through inherits
    if kind != "machine":
        for k in machine_keys:  # the CLI applies every key of every loaded file
            data.pop(k, None)
    data["from"] = "User"  # the CLI refuses system presets passed as files
    data[_ID_KEY[kind]] = [name] if kind == "filament" else name  # empty ids fail with -17
    return data


def _check_machine(nozzle: str, m: dict, profile: PrinterProfile) -> None:
    area = m.get("printable_area") or []
    if profile.bed_area not in area:
        raise AssertionError(f"unexpected bed {area}, expected {profile.bed_area}")
    if m.get("printable_height") != profile.bed_height:
        raise AssertionError(f"unexpected printable_height {m.get('printable_height')!r}")
    if m.get("nozzle_diameter") != [nozzle]:
        raise AssertionError(f"unexpected nozzle {m.get('nozzle_diameter')!r}, expected {[nozzle]}")


def _first(value: object) -> object:
    """Bambu stores most filament keys as one-element lists."""
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _temp(value: object) -> int | None:
    value = _first(value)
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        return int(value)
    except ValueError:
        return None


def filament_tray_info(preset: dict) -> dict[str, object]:
    """What the server's spool record needs for load_spool, from a flattened filament preset.

    Keys are named as the spool tool parameters (``material`` is the preset's filament_type).
    """
    filament_id = preset.get("filament_id")
    material = _first(preset.get("filament_type"))
    return {
        "tray_info_idx": filament_id if isinstance(filament_id, str) and filament_id else None,
        "nozzle_temp_min": _temp(preset.get("nozzle_temperature_range_low")),
        "nozzle_temp_max": _temp(preset.get("nozzle_temperature_range_high")),
        "material": material if isinstance(material, str) and material else None,
    }


def flatten_all(cfg: AgentConfig) -> list[Path]:
    """Write machine-<nozzle>, process-<set> and filament-<slug> JSON files; return their paths."""
    idx = _index(preset_roots(cfg))
    machines = {n: _resolve("machine", name, idx) for n, name in cfg.machine.items()}
    machine_keys: set[str] = set()
    for m in machines.values():
        machine_keys |= set(m) - _MACHINE_KEEP
    cfg.profiles_dir.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []

    def write(file: str, data: dict) -> None:
        path = cfg.profiles_dir / file
        path.write_text(json.dumps(data, indent=1), encoding="utf-8", newline="\n")
        out.append(path)

    for nozzle, name in cfg.machine.items():
        data = _finish("machine", name, machines[nozzle], machine_keys)
        _check_machine(nozzle, data, cfg.profile)
        write(f"machine-{nozzle}.json", data)
    for key, name in cfg.process_sets.items():
        write(f"process-{key}.json", _finish("process", name, _resolve("process", name, idx), machine_keys))
    for name in cfg.filaments:
        write(f"filament-{_slug(name)}.json", _finish("filament", name, _resolve("filament", name, idx), machine_keys))
    return out
