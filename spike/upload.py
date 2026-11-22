"""Phase 0 throwaway: implicit FTPS (990) upload test with verified TLS. Starts nothing.

Usage: uv run --no-project python -I spike/upload.py <local file> <remote path> [<remote path> ...]
       uv run --no-project python -I spike/upload.py --list
"""

import ftplib
import socket
import ssl
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load_env(path: Path) -> dict[str, str]:
    env = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


class ImplicitFTPS(ftplib.FTP_TLS):
    """FTP_TLS that wraps the control socket at connect and reuses the TLS session on data sockets."""

    def __init__(self, *args, server_hostname: str, **kwargs):
        super().__init__(*args, **kwargs)
        self._server_hostname = server_hostname
        self._sock = None

    @property
    def sock(self):
        return self._sock

    @sock.setter
    def sock(self, value):
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value, server_hostname=self._server_hostname)
        self._sock = value

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(conn, server_hostname=self._server_hostname, session=self.sock.session)
        return conn, size


def connect(env: dict[str, str]) -> ImplicitFTPS:
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(cadata=(HERE / "certs" / "bambu.cert").read_text())
    ftp = ImplicitFTPS(context=ctx, server_hostname=env["BAMBU_SERIAL"], timeout=30)
    print(ftp.connect(env["BAMBU_HOST"], 990))
    print(ftp.login("bblp", env["BAMBU_ACCESS_CODE"]).replace(env["BAMBU_ACCESS_CODE"], "***"))
    print(ftp.prot_p())
    return ftp


def listing(ftp: ImplicitFTPS, path: str) -> None:
    try:
        lines: list[str] = []
        ftp.retrlines(f"LIST {path}", lines.append)
        print(f"LIST {path}: {len(lines)} entries")
        for line in lines[:15]:
            print("   ", line)
    except ftplib.all_errors as e:
        print(f"LIST {path}: {e}")


def main() -> None:
    env = load_env(HERE / ".env")
    ftp = connect(env)
    try:
        print("PWD", ftp.pwd())
        listing(ftp, "/")
        listing(ftp, "/cache")
        if sys.argv[1:2] == ["--list"]:
            return
        local = Path(sys.argv[1])
        for remote in sys.argv[2:]:
            try:
                with local.open("rb") as fh:
                    print(f"STOR {remote}:", ftp.storbinary(f"STOR {remote}", fh))
                print(f"SIZE {remote}:", ftp.size(remote), "local", local.stat().st_size)
            except ftplib.all_errors as e:
                print(f"STOR {remote}: {e}")
    finally:
        try:
            ftp.quit()
        except ftplib.all_errors:
            pass


if __name__ == "__main__":
    main()
