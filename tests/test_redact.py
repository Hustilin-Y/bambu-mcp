import io
import logging

import pytest

from bambu_mcp.config import Secrets
from bambu_mcp.redact import RedactingFilter, install_redaction, redact


def test_redact_replaces_every_secret():
    assert (
        redact("rtsps://bblp:12345678@10.0.0.2:322", ["12345678"])
        == "rtsps://bblp:***@10.0.0.2:322"
    )
    assert redact("a SER1 b SER1 12345678", ["12345678", "SER1"]) == "a *** b *** ***"


def test_redact_ignores_empty_secret():
    assert redact("nothing to hide", ["", "zzz"]) == "nothing to hide"


@pytest.fixture
def captured():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger()
    old_level, old_filters = root.level, list(root.filters)
    old_factory = logging.getLogRecordFactory()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    yield stream
    root.removeHandler(handler)
    root.setLevel(old_level)
    root.filters[:] = old_filters
    logging.setLogRecordFactory(old_factory)


def _secrets():
    return Secrets(
        access_code="12345678",
        serial="SERIAL123",
        host="10.0.0.2",
        notify_token="ntok",
        client_tokens={"pc": "ctok"},
    )


def test_filter_redacts_exception_text(captured):
    install_redaction(_secrets())
    logger = logging.getLogger("bambu_mcp.test")
    try:
        raise RuntimeError("auth failed for 12345678")
    except RuntimeError:
        logger.exception("connect failed")
    out = captured.getvalue()
    assert "***" in out
    assert "12345678" not in out


def test_filter_redacts_msg_and_args(captured):
    install_redaction(_secrets())
    logger = logging.getLogger("bambu_mcp.test")
    logger.info("code %s serial %s", "12345678", "SERIAL123")
    logger.info("token ctok and %(t)s", {"t": "ntok"})
    out = captured.getvalue()
    for secret in ("12345678", "SERIAL123", "ctok", "ntok"):
        assert secret not in out


def test_filter_object_redacts_record():
    flt = RedactingFilter(["s3cret"])
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "pw %s", ("s3cret",), None)
    assert flt.filter(record) is True
    assert "s3cret" not in record.getMessage()
