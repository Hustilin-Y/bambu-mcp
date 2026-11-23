import logging

from bambu_mcp.logthrottle import Throttle

LOGGER = "bambu_mcp.test.throttle"


class Mono:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def messages(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == LOGGER]


def test_first_logged_then_at_most_hourly_with_count(caplog):
    clock = Mono()
    throttle = Throttle(logging.getLogger(LOGGER), interval_s=3600, clock=clock)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        throttle.warning("poll", "poll failed: %s", "401")
        for _ in range(5):
            clock.t += 60
            throttle.warning("poll", "poll failed: %s", "401")
        assert messages(caplog) == ["poll failed: 401"]
        clock.t = 3601
        throttle.warning("poll", "poll failed: %s", "401")
        clock.t += 1
        throttle.warning("poll", "poll failed: %s", "401")
    assert messages(caplog) == [
        "poll failed: 401",
        "poll failed: 401 (5 similar suppressed in the last hour)",
    ]


def test_keys_are_independent_and_reset_logs_again(caplog):
    clock = Mono()
    throttle = Throttle(logging.getLogger(LOGGER), interval_s=3600, clock=clock)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        throttle.warning("a", "a failed")
        throttle.warning("b", "b failed")
        throttle.warning("a", "a failed")
        throttle.reset("a")
        throttle.warning("a", "a failed")
        throttle.info("c", "c info")
    assert messages(caplog) == ["a failed", "b failed", "a failed", "c info"]
    assert caplog.records[-1].levelno == logging.INFO
