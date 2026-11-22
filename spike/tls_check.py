"""Phase 0 throwaway: does the printer's TLS chain verify against Bambu's CAs?

Usage: uv run --no-project python -I spike/tls_check.py
"""

import socket
import ssl
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load_env(path: Path) -> dict[str, str]:
    env = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def attempt(label: str, host: str, port: int, cafiles: list[Path], server_hostname: str | None, check_hostname: bool) -> None:
    ctx = ssl.create_default_context(cafile=None)
    ctx.load_verify_locations(cadata="\n".join(p.read_text().strip() for p in cafiles) + "\n")
    ctx.check_hostname = check_hostname
    ctx.verify_mode = ssl.CERT_REQUIRED
    try:
        with socket.create_connection((host, port), timeout=10) as sock:
            with ctx.wrap_socket(sock, server_hostname=server_hostname) as tls:
                print(f"OK   {label}: {tls.version()} peer CN={dict(x[0] for x in tls.getpeercert()['subject'])['commonName']}")
    except ssl.SSLError as e:
        print(f"FAIL {label}: {e.reason or e}")
    except OSError as e:
        print(f"FAIL {label}: {e}")


def main() -> None:
    env = load_env(HERE / ".env")
    host, serial = env["BAMBU_HOST"], env["BAMBU_SERIAL"]
    certs = HERE / "certs"
    full = [certs / "bambu.cert", certs / "bambu_p2s_250626.cert"]
    for port in (8883, 990):
        attempt(f"{port} root+P2S CA, hostname=serial", host, port, full, serial, True)
        attempt(f"{port} root+P2S CA, no hostname check", host, port, full, serial, False)
        attempt(f"{port} root only, no hostname check", host, port, [certs / "bambu.cert"], serial, False)


if __name__ == "__main__":
    main()
