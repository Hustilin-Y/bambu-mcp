"""Upload a sliced file to the printer's USB stick over implicit FTPS (port 990).

The control socket is wrapped in TLS at connect, before the greeting is read,
with the same context rules as the MQTT link (``link.build_tls_context``: chain
verified against the CA file, peer CN == printer serial, checked inside the
handshake), so the access code is never sent to an unverified peer. The CN is
checked a second time after connect, before login. The printer serves FTPS
over TLS 1.2 (phase 0), and data sockets reuse the control TLS session, which
the printer requires.

The file goes to the stick's root, then ``SIZE`` must equal the local size.
Nothing is ever deleted from the stick. Uploads are serialised: one at a time
per process. Blocking: the service runs ``upload`` in a worker thread.
"""

from __future__ import annotations

import ftplib
import logging
import ssl
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

from bambu_mcp.config import Settings
from bambu_mcp.printer.link import _verify_peer, build_tls_context

log = logging.getLogger(__name__)

FTPS_PORT: Final = 990
FTPS_USER: Final = "bblp"
_MAX_NAME = 255
_UPLOAD_LOCK = threading.Lock()


class UploadError(Exception):
    """The upload failed. The message is English and carries no secrets.

    ``text`` is the same for the owner: Russian, plain words, safe in Telegram Markdown.
    """

    def __init__(self, message: str, text: str = "загрузка на принтер не удалась") -> None:
        super().__init__(message)
        self.text = text


class ImplicitFTPS(ftplib.FTP_TLS):
    """FTP_TLS that wraps the control socket at connect and reuses its TLS session on data sockets."""

    def __init__(self, *args: Any, server_hostname: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._server_hostname = server_hostname
        self._sock: Any = None

    @property
    def sock(self) -> Any:
        return self._sock

    @sock.setter
    def sock(self, value: Any) -> None:
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value, server_hostname=self._server_hostname)
        self._sock = value

    def ntransfercmd(self, cmd: str, rest: Any = None) -> tuple[Any, int | None]:
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(
                conn, server_hostname=self._server_hostname, session=self.sock.session
            )
        return conn, size


def build_ftps_context(ca_file: Path, serial: str) -> ssl.SSLContext:
    """The MQTT link's ``ca`` context (CA chain + CN == serial), capped at TLS 1.2."""
    ctx = build_tls_context(Settings(tls_mode="ca", ca_file=Path(ca_file)), serial=serial)
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def _check_remote_name(name: str) -> None:
    if (
        not name
        or name in (".", "..")
        or len(name) > _MAX_NAME
        or any(c in name for c in "/\\")
        or any(ord(c) < 32 or ord(c) == 127 for c in name)
    ):
        raise ValueError("remote_name must be a plain file name")


def _reply_code(err: BaseException) -> str:
    code = str(err)[:3]
    return code if code.isdigit() else "unknown"


def _close(ftp: Any) -> None:
    if getattr(ftp, "sock", None) is None:  # connect failed: nothing to quit, and quit() would raise
        return
    try:
        ftp.quit()
    except (*ftplib.all_errors, AttributeError):
        try:
            ftp.close()
        except ftplib.all_errors:
            pass


def upload(
    host: str,
    serial: str,
    access_code: str,
    ca_file: Path,
    local: Path,
    remote_name: str,
    *,
    timeout_s: float = 60,
    ftp_factory: Callable[..., Any] = ImplicitFTPS,
) -> None:
    """Stores ``local`` as ``/<remote_name>`` on the printer's USB stick and checks its size.

    Raises ``UploadError`` on any failure (``ValueError`` for an unsafe name).
    """
    _check_remote_name(remote_name)
    try:
        local_size = Path(local).stat().st_size
    except OSError:
        raise UploadError("local file to upload is missing", "файла для загрузки нет на сервере") from None
    try:
        context = build_ftps_context(ca_file, serial)
    except (ValueError, OSError, ssl.SSLError):
        raise UploadError(
            "FTPS needs a readable CA file and the printer serial",
            "на сервере нет файла сертификатов или серийного номера принтера",
        ) from None

    remote = f"/{remote_name}"
    with _UPLOAD_LOCK:
        ftp = ftp_factory(context=context, server_hostname=serial, timeout=timeout_s)
        try:
            _transfer(ftp, host, serial, access_code, Path(local), remote, local_size)
        finally:
            _close(ftp)
    log.info("uploaded %s to the printer (%d bytes)", remote_name, local_size)


def _transfer(
    ftp: Any, host: str, serial: str, access_code: str, local: Path, remote: str, local_size: int
) -> None:
    # Every except clause raises "from None": the original messages may echo
    # the serial or the access code.
    try:
        ftp.connect(host, FTPS_PORT)
    except ssl.SSLCertVerificationError:
        raise UploadError(
            "printer certificate check failed, refusing to log in", "сертификат принтера не прошёл проверку"
        ) from None
    except ssl.SSLError:
        raise UploadError("TLS handshake with the printer failed", "не удалось установить защищённое соединение с принтером") from None
    except ftplib.Error:
        raise UploadError("unexpected FTPS greeting from the printer", "принтер ответил не так, как ожидалось") from None
    except (OSError, EOFError):
        raise UploadError("cannot reach the printer's FTPS port", "принтер не отвечает, файл не передать") from None

    try:
        _verify_peer(ftp.sock, "ca", serial, None)
    except (ssl.SSLCertVerificationError, ValueError, AttributeError):
        raise UploadError(
            "printer certificate CN does not match, refusing to log in",
            "сертификат принтера выдан на другой серийный номер",
        ) from None

    try:
        ftp.login(FTPS_USER, access_code)
        ftp.prot_p()
    except ftplib.error_perm:
        raise UploadError(
            "printer refused the FTPS login (check the access code)", "принтер не принял код доступа"
        ) from None
    except ftplib.Error as err:
        raise UploadError(
            f"FTPS login failed (reply {_reply_code(err)})", f"вход на принтер не удался (код {_reply_code(err)})"
        ) from None
    except (OSError, EOFError):
        raise UploadError(
            "connection to the printer lost during FTPS login", "связь с принтером прервалась при входе"
        ) from None

    try:
        with local.open("rb") as fh:
            ftp.storbinary(f"STOR {remote}", fh)
    except ftplib.error_perm as err:
        code = _reply_code(err)
        if code == "553":
            raise UploadError(
                "printer cannot store the file (553): no USB storage in the printer?",
                "принтер не может сохранить файл, вставлена ли флешка?",
            ) from None
        raise UploadError(f"printer refused the upload (reply {code})", f"принтер не принял файл (код {code})") from None
    except ftplib.Error as err:
        raise UploadError(
            f"upload failed (reply {_reply_code(err)})", f"передача файла не удалась (код {_reply_code(err)})"
        ) from None
    except (OSError, EOFError):
        raise UploadError(
            "connection to the printer lost during upload", "связь с принтером прервалась во время передачи"
        ) from None

    try:
        remote_size = ftp.size(remote)
    except ValueError:
        raise UploadError(
            "the printer sent an unreadable size reply", "принтер прислал непонятный размер файла"
        ) from None
    except ftplib.Error as err:
        raise UploadError(
            f"cannot read the uploaded size (reply {_reply_code(err)})",
            f"не удалось узнать размер переданного файла (код {_reply_code(err)})",
        ) from None
    except (OSError, EOFError):
        raise UploadError(
            "connection lost while checking the uploaded size", "связь с принтером прервалась при проверке размера"
        ) from None
    if remote_size != local_size:
        raise UploadError(
            f"uploaded size {remote_size} differs from the local size {local_size}",
            f"размер файла на принтере {remote_size} не совпадает с исходным {local_size}",
        )
