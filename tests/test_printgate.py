import sqlite3
import threading
from pathlib import Path

import pytest

from bambu_mcp.printgate import CONFIRMATION_TTL_S, gate_hash, new_confirmation_id
from bambu_mcp.store.db import Store

_BASE = dict(
    file_sha256="a" * 64,
    plate=1,
    ams=0,
    slot=2,
    serial="SERIAL01",
    nozzle="0.4",
    plate_type="Textured PEI Plate",
)
_OTHER = dict(
    file_sha256="b" * 64,
    plate=2,
    ams=1,
    slot=3,
    serial="SERIAL02",
    nozzle="0.6",
    plate_type="Cool Plate",
)


@pytest.fixture
def store(tmp_path: Path):
    s = Store(tmp_path / "bambu.db")
    s.migrate()
    yield s
    s.close()


def test_gate_hash_is_stable_sha256_hex():
    h = gate_hash(**_BASE)
    assert h == gate_hash(**_BASE)
    assert len(h) == 64 and int(h, 16) >= 0


@pytest.mark.parametrize("field", sorted(_BASE))
def test_gate_hash_changes_with_each_field(field):
    changed = {**_BASE, field: _OTHER[field]}
    assert gate_hash(**changed) != gate_hash(**_BASE)


def test_gate_hash_does_not_mix_fields():
    # ams 1 slot 2 must not hash like ams 12 slot nothing, or swapped values.
    assert gate_hash(**{**_BASE, "ams": 1, "slot": 2}) != gate_hash(**{**_BASE, "ams": 2, "slot": 1})


def test_new_confirmation_id_is_128_bit_hex_and_random():
    ids = {new_confirmation_id() for _ in range(100)}
    assert len(ids) == 100
    for cid in ids:
        assert len(cid) == 32 and int(cid, 16) >= 0


def _add(store: Store, now: float = 1000.0, kind: str = "start"):
    return store.add_confirmation(
        new_confirmation_id(),
        kind,
        job_id=None,
        plate=1,
        gate_hash=gate_hash(**_BASE),
        payload_json='{"ams": 0, "slot": 2}',
        now=now,
    )


def test_add_confirmation_round_trip(store: Store):
    c = _add(store)
    assert c.state == "pending"
    assert c.outcome is None
    assert c.created_at == 1000.0
    assert c.expires_at == 1000.0 + CONFIRMATION_TTL_S == 1600.0
    assert store.confirmation(c.id) == c
    assert store.confirmation("nope") is None


def test_add_confirmation_rejects_unknown_kind(store: Store):
    with pytest.raises(sqlite3.IntegrityError):
        _add(store, kind="resume")


def test_claim_is_single_use(store: Store):
    c = _add(store)
    claimed = store.claim_confirmation(c.id, now=1001.0)
    assert claimed is not None
    assert claimed.id == c.id and claimed.state == "used"
    assert store.claim_confirmation(c.id, now=1002.0) is None
    assert store.confirmation(c.id).state == "used"


def test_claim_is_single_use_across_two_stores_on_one_file(tmp_path: Path):
    path = tmp_path / "shared.db"
    a, b = Store(path), Store(path)
    a.migrate()
    b.migrate()
    c = _add(a)
    results: list = []
    barrier = threading.Barrier(2)

    def claim(s: Store) -> None:
        barrier.wait()
        results.append(s.claim_confirmation(c.id, now=1001.0))

    threads = [threading.Thread(target=claim, args=(s,)) for s in (a, b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(r is not None for r in results) == 1
    # And sequentially: whichever store asks next gets nothing.
    assert a.claim_confirmation(c.id, now=1002.0) is None
    assert b.claim_confirmation(c.id, now=1002.0) is None
    a.close()
    b.close()


def test_claim_after_expiry_is_none_and_marks_expired(store: Store):
    c = _add(store)
    assert store.claim_confirmation(c.id, now=1000.0 + CONFIRMATION_TTL_S) is None
    assert store.confirmation(c.id).state == "expired"
    # Still None afterwards, even with an earlier clock.
    assert store.claim_confirmation(c.id, now=1001.0) is None
    assert store.confirmation(c.id).state == "expired"


def test_claim_just_before_expiry_wins(store: Store):
    c = _add(store)
    assert store.claim_confirmation(c.id, now=1000.0 + CONFIRMATION_TTL_S - 0.001) is not None


def test_claim_unknown_is_none(store: Store):
    assert store.claim_confirmation("0" * 32, now=1001.0) is None


def test_cancelled_cannot_be_claimed(store: Store):
    c = _add(store)
    assert store.cancel_confirmation(c.id) is True
    assert store.confirmation(c.id).state == "cancelled"
    assert store.claim_confirmation(c.id, now=1001.0) is None
    assert store.confirmation(c.id).state == "cancelled"


def test_cancel_does_not_touch_a_used_confirmation(store: Store):
    c = _add(store)
    assert store.claim_confirmation(c.id, now=1001.0) is not None
    assert store.cancel_confirmation(c.id) is False
    assert store.confirmation(c.id).state == "used"
    assert store.cancel_confirmation("unknown") is False


def test_claim_inside_an_outer_transaction_rolls_back_with_it(store: Store):
    c = _add(store)
    with pytest.raises(RuntimeError):
        with store.transaction():
            assert store.claim_confirmation(c.id, now=1001.0) is not None
            raise RuntimeError("boom")
    assert store.confirmation(c.id).state == "pending"
