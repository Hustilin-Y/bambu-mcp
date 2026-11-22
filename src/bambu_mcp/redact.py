"""Secret redaction for log output."""

from __future__ import annotations

import logging
from collections.abc import Iterable

MASK = "***"


def redact(text: str, secrets: Iterable[str]) -> str:
    """Replace every non-empty secret in ``text`` with ``***``."""
    # Longest first so a secret that contains another is masked whole.
    for secret in sorted({s for s in secrets if s}, key=len, reverse=True):
        text = text.replace(secret, MASK)
    return text


class RedactingFilter(logging.Filter):
    """Rewrites msg, args and exception text of every record it sees."""

    def __init__(self, secrets: Iterable[str]) -> None:
        super().__init__()
        self._secrets = tuple(s for s in secrets if s)

    def _clean(self, value):
        if isinstance(value, str):
            return redact(value, self._secrets)
        text = str(value)
        cleaned = redact(text, self._secrets)
        return cleaned if cleaned != text else value

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        # Keep the args structure (formatters such as uvicorn's access
        # formatter unpack record.args), redacting items in place.
        original_msg, original_args = record.msg, record.args
        try:
            record.msg = self._clean(record.msg)
            if isinstance(record.args, tuple):
                record.args = tuple(self._clean(a) for a in record.args)
            elif isinstance(record.args, dict):
                record.args = {k: self._clean(v) for k, v in record.args.items()}
            message = record.getMessage()
            cleaned = redact(message, self._secrets)
            if cleaned != message:  # a secret only appears once formatted
                record.msg, record.args = cleaned, None
        except Exception:
            # Malformed log call: never raise from the record factory, the
            # standard path reports it through Handler.handleError.
            record.msg = redact(f"{original_msg!s} {original_args!r}", self._secrets)
            record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text, self._secrets)
        if record.stack_info:
            record.stack_info = redact(record.stack_info, self._secrets)
        return True


def install_redaction(secrets) -> None:
    """Install the filter process-wide.

    A filter on the root logger only sees records logged directly to it, and
    handler filters miss handlers added later. So the filter also runs from the
    LogRecord factory, which every record passes through regardless of logger
    or handler.
    """
    values = [
        secrets.access_code,
        secrets.serial,
        secrets.notify_token,
        *secrets.client_tokens.values(),
    ]
    flt = RedactingFilter(v for v in values if v)
    logging.getLogger().addFilter(flt)

    previous = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        flt.filter(record)
        return record

    logging.setLogRecordFactory(factory)
