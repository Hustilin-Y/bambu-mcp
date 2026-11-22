"""Phase 0 throwaway: pick representative raw reports and write redacted fixtures to tests/fixtures/p2s/.

Usage: uv run --no-project python -I spike/make_fixtures.py
"""

import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
RAW = HERE / "raw"
OUT = HERE.parent / "tests" / "fixtures" / "p2s"


def load_env(path: Path) -> dict[str, str]:
    env = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def redact(obj, secrets: dict[str, str]):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in ("sn", "ams_sn", "dev_id") and isinstance(v, str) and v not in ("", "N/A"):
                out[k] = "SN"
            elif k in ("ip", "mask", "gw") and isinstance(v, int):
                out[k] = 0
            elif k == "_received_at":
                continue
            else:
                out[k] = redact(v, secrets)
        return out
    if isinstance(obj, list):
        return [redact(v, secrets) for v in obj]
    if isinstance(obj, str):
        for secret, placeholder in secrets.items():
            obj = obj.replace(secret, placeholder)
        return obj
    return obj


def main() -> None:
    env = load_env(HERE / ".env")
    secrets = {env["BAMBU_ACCESS_CODE"]: "ACCESS", env["BAMBU_SERIAL"]: "SERIAL", env["BAMBU_HOST"]: "10.0.0.2"}
    files = sorted(RAW.glob("0*.json"))
    msgs = [(f, json.loads(f.read_text(encoding="utf-8"))) for f in files]

    def first(pred):
        return next(((f, d) for f, d in msgs if pred(d.get("print", {}), d)), None)

    pick = {
        "version": first(lambda p, d: "info" in d and "module" in d["info"]),
        "pushall_idle": first(lambda p, d: p.get("gcode_state") == "FINISH" and len(p) > 50),
        "running_layer0": first(lambda p, d: p.get("subtask_name") == "cube" and p.get("gcode_state") == "RUNNING" and p.get("layer_num") == 0),
        "running_layer1": first(lambda p, d: p.get("subtask_name") == "cube" and p.get("layer_num") == 1),
        "running_layer2": first(lambda p, d: p.get("subtask_name") == "cube" and p.get("layer_num") == 2),
        "running_mid": first(lambda p, d: p.get("subtask_name") == "cube" and p.get("layer_num") == 50),
        "finish": first(lambda p, d: p.get("subtask_name") == "cube" and p.get("gcode_state") == "FINISH"),
        "gcode_line": first(lambda p, d: p.get("command") == "gcode_line"),
    }
    OUT.mkdir(parents=True, exist_ok=True)
    blob_secrets = list(secrets)
    for name, hit in pick.items():
        if hit is None:
            print(f"{name}: not found")
            continue
        f, data = hit
        clean = redact(data, secrets)
        text = json.dumps(clean, indent=1, ensure_ascii=False)
        leaked = [s for s in blob_secrets if s in text]
        assert not leaked, f"{name}: secret left in fixture"
        assert not re.search(r"192\.168\.\d+\.\d+", text), f"{name}: LAN IP left"
        (OUT / f"{name}.json").write_text(text + "\n", encoding="utf-8")
        print(f"{name}: from {f.name}, {len(text)} chars")


if __name__ == "__main__":
    main()
