"""Phase 0 throwaway: read-only MQTT probe. Dumps every report to spike/raw/<n>.json.

Publishes only get_version and pushall. Usage:
    uv run --with paho-mqtt python -I spike/probe.py [seconds] [pushall_every_s]
"""

import json
import ssl
import sys
import time
from pathlib import Path

import paho.mqtt.client as mqtt

HERE = Path(__file__).resolve().parent
RAW = HERE / "raw"
GET_VERSION = {"info": {"sequence_id": "0", "command": "get_version"}}
PUSH_ALL = {"pushing": {"sequence_id": "0", "command": "pushall"}}


def load_env(path: Path) -> dict[str, str]:
    env = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def main() -> None:
    duration = float(sys.argv[1]) if len(sys.argv) > 1 else 120
    pushall_every = float(sys.argv[2]) if len(sys.argv) > 2 else 0
    env = load_env(HERE / ".env")
    host, serial, code = env["BAMBU_HOST"], env["BAMBU_SERIAL"], env["BAMBU_ACCESS_CODE"]
    RAW.mkdir(exist_ok=True)
    start_n = len(list(RAW.glob("*.json")))
    counter = {"n": start_n}

    def on_connect(client, userdata, flags, reason_code, properties):
        print(f"connect: {reason_code}")
        if reason_code.is_failure:
            return
        client.subscribe(f"device/{serial}/report")
        for msg in (GET_VERSION, PUSH_ALL):
            client.publish(f"device/{serial}/request", json.dumps(msg))

    def on_message(client, userdata, msg):
        counter["n"] += 1
        path = RAW / f"{counter['n']:05d}.json"
        try:
            data = json.loads(msg.payload)
        except ValueError:
            path.with_suffix(".bin").write_bytes(msg.payload)
            print(f"{path.name}: non-JSON, {len(msg.payload)} bytes")
            return
        data["_received_at"] = time.time()
        path.write_text(json.dumps(data, indent=1), encoding="utf-8")
        p = data.get("print", {})
        print(f"{path.name}: keys={list(data)[:4]} state={p.get('gcode_state')} layer={p.get('layer_num')} cmd={p.get('command')}")

    def on_disconnect(client, userdata, flags, reason_code, properties):
        print(f"disconnect: {reason_code}")

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"probe-{int(time.time())}")
    client.username_pw_set("bblp", code)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # TLS verification is tested separately (Task 3)
    client.tls_set_context(ctx)
    client.on_connect, client.on_message, client.on_disconnect = on_connect, on_message, on_disconnect
    client.connect(host, 8883, keepalive=60)
    client.loop_start()
    end, last_push = time.time() + duration, time.time()
    try:
        while time.time() < end:
            time.sleep(1)
            if pushall_every and time.time() - last_push >= pushall_every:
                client.publish(f"device/{serial}/request", json.dumps(PUSH_ALL))
                last_push = time.time()
    finally:
        client.loop_stop()
        client.disconnect()
    print(f"saved {counter['n'] - start_n} messages to {RAW}")


if __name__ == "__main__":
    main()
