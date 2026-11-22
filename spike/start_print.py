"""Phase 0 throwaway: start ONE print of a file already on the printer's USB root, then watch reports.

Run only with the owner at the printer and the bed clear.
Usage: uv run --no-project --with paho-mqtt python -I spike/start_print.py <remote name> <md5> <ams slot> [watch_s]
"""

import json
import ssl
import sys
import time
from pathlib import Path

import paho.mqtt.client as mqtt

HERE = Path(__file__).resolve().parent


def load_env(path: Path) -> dict[str, str]:
    env = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def main() -> None:
    remote, md5, slot = sys.argv[1], sys.argv[2], int(sys.argv[3])
    watch = float(sys.argv[4]) if len(sys.argv) > 4 else 60
    env = load_env(HERE / ".env")
    serial = env["BAMBU_SERIAL"]
    task_id = str(int(time.time()))
    subtask = remote.removesuffix(".gcode.3mf")
    payload = {"print": {
        "sequence_id": "1", "command": "project_file",
        "param": "Metadata/plate_1.gcode",
        "url": f"ftp:///{remote}", "file": remote, "md5": md5,
        "subtask_name": subtask, "project_id": "0", "profile_id": "0",
        "task_id": task_id, "subtask_id": "0",
        "timelapse": False, "bed_type": "auto",
        "bed_leveling": True, "bed_levelling": True, "auto_bed_leveling": 2,
        "flow_cali": False, "vibration_cali": False, "layer_inspect": True,
        "use_ams": True,
        "ams_mapping": [slot],
        "ams_mapping2": [{"ams_id": 0, "slot_id": slot}],
    }}
    sent = {"done": False}
    last = {}

    def on_connect(client, userdata, flags, reason_code, properties):
        print("connect:", reason_code)
        peer = client.socket().getpeercert()
        cn = dict(x[0] for x in peer["subject"])["commonName"]
        if cn != serial:
            print(f"peer CN {cn!r} != serial, refusing to send")
            client.disconnect()
            return
        client.subscribe(f"device/{serial}/report")
        if not sent["done"]:
            client.publish(f"device/{serial}/request", json.dumps(payload))
            sent["done"] = True
            print(f"sent project_file {remote} task_id={task_id} slot={slot}")

    def on_message(client, userdata, msg):
        p = json.loads(msg.payload).get("print", {})
        if p.get("command") == "project_file":
            print("ack:", {k: p.get(k) for k in ("result", "reason", "err_code", "sequence_id")})
        cur = {k: p.get(k, last.get(k)) for k in ("gcode_state", "subtask_name", "task_id", "print_error", "mc_print_stage", "layer_num")}
        cur["hms"] = p.get("hms", last.get("hms"))
        if cur != last:
            print(f"{time.strftime('%H:%M:%S')} {cur}")
            last.update(cur)

    ctx = ssl.create_default_context()
    ctx.load_verify_locations(cadata=(HERE / "certs" / "bambu.cert").read_text())
    ctx.check_hostname = False  # paho passes the IP as hostname; the chain is still verified, CN checked in on_connect
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"spike-start-{task_id}")
    client.username_pw_set("bblp", env["BAMBU_ACCESS_CODE"])
    client.tls_set_context(ctx)
    client.on_connect, client.on_message = on_connect, on_message
    client.connect(env["BAMBU_HOST"], 8883, keepalive=60)
    client.loop_start()
    time.sleep(watch)
    client.loop_stop()
    client.disconnect()


if __name__ == "__main__":
    main()
