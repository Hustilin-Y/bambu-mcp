"""Extract HMS texts for one printer model from an installed Bambu Studio.

Bambu Studio keeps one table per model, named by the printer serial prefix
(P2S: ``22E``) as ``hms_en_<prefix>.json``. Two copies exist:
``%APPDATA%/BambuStudio/hms/`` (updated) and
``C:/Program Files/Bambu Studio/resources/hms/`` (bundled).

Usage: extract_hms.py [--prefix 22E] -o OUT.json SOURCE [SOURCE ...]

Each SOURCE is a Studio data dir, an install dir, or the JSON file itself.
A source under %APPDATA% wins over the others, otherwise the first one given.
Output: {"hms": {"AAAA_AAAA_CCCC_CCCC": text}, "errors": {"0500C010": text}},
only non-empty texts (an empty intro means Bambu hides the code). The output
is third-party data: keep it in data_dir, never commit it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _look_in(source: Path, name: str) -> Path | None:
    if source.is_file():
        return source if source.name == name else None
    if not source.is_dir():
        return None
    for candidate in (source / name, source / "hms" / name, source / "resources" / "hms" / name):
        if candidate.is_file():
            return candidate
    return next(source.rglob(name), None)


def find_source(sources: list[Path], prefix: str) -> Path:
    name = f"hms_en_{prefix}.json"
    appdata = os.environ.get("APPDATA")
    if appdata:
        root = Path(appdata).resolve()
        sources = sorted(sources, key=lambda s: root not in s.resolve().parents and s.resolve() != root)
    for source in sources:
        found = _look_in(source, name)
        if found is not None:
            return found
    raise FileNotFoundError(f"{name} not found in: {', '.join(str(s) for s in sources)}")


def _texts(entries: list[dict]) -> dict[str, str]:
    return {e["ecode"]: e["intro"].strip() for e in entries if e.get("ecode") and e.get("intro", "").strip()}


def extract(path: Path) -> dict[str, dict[str, str]]:
    data = json.loads(path.read_text(encoding="utf-8"))["data"]
    hms = {}
    for ecode, text in _texts(data.get("device_hms", {}).get("en", [])).items():
        hms[f"{ecode[0:4]}_{ecode[4:8]}_{ecode[8:12]}_{ecode[12:16]}".upper()] = text
    errors = {k.upper(): v for k, v in _texts(data.get("device_error", {}).get("en", [])).items()}
    return {"hms": hms, "errors": errors}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("sources", nargs="+", type=Path)
    parser.add_argument("-o", "--out", required=True, type=Path)
    parser.add_argument("--prefix", default="22E")
    args = parser.parse_args(argv)
    try:
        source = find_source(args.sources, args.prefix)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1
    table = extract(source)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(table, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    print(f"{source}: {len(table['hms'])} hms, {len(table['errors'])} errors -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
