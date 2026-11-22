"""Phase 0 throwaway: grab one camera frame with PyAV, report peak memory. The URL never leaves this process.

Usage: uv run --no-project --with av --with psutil python -I spike/camera.py
"""

import os
import time
from pathlib import Path

import av
import psutil

HERE = Path(__file__).resolve().parent


def load_env(path: Path) -> dict[str, str]:
    env = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def main() -> None:
    env = load_env(HERE / ".env")
    code = env["BAMBU_ACCESS_CODE"]
    url = f"rtsps://bblp:{code}@{env['BAMBU_HOST']}:322/streaming/live/1"
    proc = psutil.Process(os.getpid())
    rss0 = proc.memory_info().rss
    t0 = time.time()
    try:
        with av.open(url, options={"rtsp_transport": "tcp", "tls_verify": "0", "timeout": "10000000"}, timeout=15) as c:
            for frame in c.decode(video=0):
                img = frame.to_image()
                out = HERE / "raw" / "snapshot.jpg"
                img.save(out, quality=85)
                print(f"frame {frame.width}x{frame.height} saved to {out.name} in {time.time() - t0:.1f} s")
                break
    except Exception as e:  # noqa: BLE001
        print("error:", str(e).replace(code, "***"))
    print(f"rss before {rss0 / 1e6:.0f} MB, after {proc.memory_info().rss / 1e6:.0f} MB")


if __name__ == "__main__":
    main()
