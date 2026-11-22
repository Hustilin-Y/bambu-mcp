"""Phase 0 throwaway: flatten Bambu Studio presets (resolve `inherits`) into full JSON for the CLI.

Usage: uv run --no-project python -I spike/flatten.py
Writes spike/profiles/{machine,process,filament}.json (git-ignored: AGPL data, follows the installed Studio).
"""

import json
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ["APPDATA"]) / "BambuStudio"
KINDS = ("machine", "process", "filament")
WANT = {
    "machine": "Bambu Lab P2S 0.4 nozzle",
    "process": "0.20mm Standard @BBL P2S",
    "filament": "Generic PETG @BBL P2S",
}


def index() -> dict[str, dict[str, Path]]:
    idx: dict[str, dict[str, Path]] = {k: {} for k in KINDS}
    for kind in KINDS:
        for base in [ROOT / "system" / "BBL" / kind, *sorted((ROOT / "user").glob(f"*/{kind}"))]:
            for f in base.glob("*.json"):
                try:
                    name = json.loads(f.read_text(encoding="utf-8")).get("name", f.stem)
                except (ValueError, UnicodeDecodeError):
                    continue
                idx[kind].setdefault(name, f)
    return idx


def resolve(kind: str, name: str, idx, seen=()) -> dict:
    if name in seen:
        raise ValueError(f"inherits loop: {seen + (name,)}")
    data = json.loads(idx[kind][name].read_text(encoding="utf-8"))
    parent = data.get("inherits")
    merged = resolve(kind, parent, idx, seen + (name,)) if parent else {}
    # Order: parent < included templates < own keys
    for inc in data.get("include", []):
        tpl = resolve(kind, inc, idx, seen + (name,))
        merged.update({k: v for k, v in tpl.items() if k not in ("name", "type", "from", "instantiation", "setting_id")})
    merged.update(data)
    merged.pop("inherits", None)
    merged.pop("include", None)
    return merged


def main() -> None:
    idx = index()
    out = HERE / "profiles"
    out.mkdir(exist_ok=True)
    machine_keys = set(resolve("machine", WANT["machine"], idx)) - {"name", "inherits", "compatible_printers", "from", "type", "setting_id", "instantiation"}
    for kind, name in WANT.items():
        data = resolve(kind, name, idx)
        data["inherits"] = name  # the CLI identifies the system preset through inherits
        if kind != "machine":
            for k in machine_keys:  # the CLI applies every key of every loaded file
                data.pop(k, None)
        data["from"] = "User"  # the CLI refuses system presets passed as files
        id_key = {"machine": "printer_settings_id", "process": "print_settings_id", "filament": "filament_settings_id"}[kind]
        data[id_key] = [name] if kind == "filament" else name  # empty ids fail the compatibility check (-17)
        (out / f"{kind}.json").write_text(json.dumps(data, indent=1), encoding="utf-8")
        print(f"{kind}: {name} -> {len(data)} keys")
    m = json.loads((out / "machine.json").read_text(encoding="utf-8"))
    print("printable_area", m.get("printable_area"), "height", m.get("printable_height"), "nozzle", m.get("nozzle_diameter"))
    area = m.get("printable_area", [])
    assert area and area[-2:] != [] and "256x256" in area, f"unexpected bed {area}"
    assert m.get("printable_height") == "256", m.get("printable_height")
    assert m.get("nozzle_diameter") == ["0.4"], m.get("nozzle_diameter")
    print("assertions OK")


if __name__ == "__main__":
    main()
