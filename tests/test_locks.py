import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.config import Settings
from app.db import DB
from app.locks import (BACKUP_NAME, DEFAULTS, LockManager, desired_codes, next_check, plan)
from app.reservations import normalize
from app.sync import Syncer

TZ = ZoneInfo("America/New_York")
SETTINGS = Settings(hostaway_account_id="1", hostaway_api_key="x", ha_url="http://ha", ha_token="t",
                    db_path=":memory:", is_addon=False)


def at(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=TZ)


def res(id=1, arrival="2030-01-10", departure="2030-01-15", code="4821", check_in=16, status="new"):
    return {"id": id, "listingMapId": 100, "status": status, "guestName": "Ann Lee",
            "arrivalDate": arrival, "departureDate": departure, "checkInTime": check_in,
            "checkOutTime": 10, "doorCode": code}


def norm(**kw):
    return normalize(res(**kw), TZ, 15, 10)


# ---- pure planning ---------------------------------------------------------

def test_desired_codes_window():
    r = [norm()]
    assert desired_codes(r, at("2030-01-10T07:59"), 8, 3) == {}
    assert desired_codes(r, at("2030-01-10T08:00"), 8, 3) == {"HA-1": "4821"}
    assert desired_codes(r, at("2030-01-15T09:59"), 8, 3) == {"HA-1": "4821"}
    assert desired_codes(r, at("2030-01-15T10:00"), 8, 3) == {}
    early = [norm(check_in=9)]  # 9 AM check-in: code goes in at 6 AM
    assert desired_codes(early, at("2030-01-10T06:00"), 8, 3) == {"HA-1": "4821"}
    assert desired_codes([norm(status="cancelled")], at("2030-01-12T12:00"), 8, 3) == {}
    assert desired_codes([norm(code=None)], at("2030-01-12T12:00"), 8, 3) == {}
    assert desired_codes([], at("2030-01-12T12:00"), 8, 3, backup_code="5555") == {BACKUP_NAME: "5555"}


def test_plan():
    p = plan({"HA-1": "4821"}, {"Master": "9999"})
    assert p.add == {"HA-1": "4821"} and p.delete == []
    p = plan({}, {"Master": "9999", "HA-7": "1111"})
    assert p.delete == ["HA-7"] and not p.add  # never touches codes we did not write
    p = plan({"HA-1": "4821"}, {"HA-1": "1111"})
    assert p.delete == ["HA-1"] and p.add == {"HA-1": "4821"}
    p = plan({"HA-1": "4821"}, {"Ann Hostaway": "4821"})
    assert p.ok and p.elsewhere == ["HA-1"]  # Hostaway already wrote it
    p = plan({"HA-2": "4821"}, {"HA-1": "4821"})  # value only held by a code we are removing
    assert p.delete == ["HA-1"] and p.add == {"HA-2": "4821"}
    assert plan({"HA-1": "4821"}, {"HA-1": "4821", "Master": "9"}).ok


def test_next_check_picks_the_nearest_event():
    r = [norm()]
    cfg = dict(DEFAULTS)
    assert next_check(r, at("2030-01-09T12:00"), cfg) == at("2030-01-10T06:00")  # daily check
    assert next_check(r, at("2030-01-10T07:00"), cfg) == at("2030-01-10T08:00")  # add
    assert next_check(r, at("2030-01-10T08:05"), cfg) == at("2030-01-10T14:00")  # afternoon
    assert next_check(r, at("2030-01-10T14:05"), cfg) == at("2030-01-10T15:00")  # final check
    assert next_check(r, at("2030-01-15T07:00"), cfg) == at("2030-01-15T10:00")  # checkout


# ---- reconcile against a fake lock ------------------------------------------

class FakeLock:
    """Behaves like the Schlage integration: optional silent failures and timeouts that still work."""

    def __init__(self):
        self.codes = {"lock.a": {"Master": "9999"}}
        self.broken_add = set()
        self.delete_times_out = False
        self.read_fails = False
        self.notes = []

    async def get_codes(self, entity_id):
        if self.read_fails:
            raise TimeoutError("schlage timeout")
        return dict(self.codes[entity_id])

    async def add_code(self, entity_id, name, code):
        if name not in self.broken_add:
            self.codes[entity_id][name] = code

    async def delete_code(self, entity_id, name):
        self.codes[entity_id].pop(name, None)
        if self.delete_times_out:
            raise TimeoutError()

    async def notify(self, title, message, service="", notification_id=""):
        self.notes.append((title, message))


class FakeHostaway:
    def __init__(self):
        self.sent = []

    async def send_guest_message(self, reservation_id, body):
        self.sent.append((reservation_id, body))


@pytest.fixture
def lm():
    db = DB(":memory:")
    s = Syncer(db, SETTINGS, FakeHostaway(), FakeLock())
    pid = db.execute("INSERT INTO properties(hostaway_listing_id, hostaway_name, name, lock_automation) "
                     "VALUES(100, 'Maple 1', 'Maple 1', 1)")
    db.execute("INSERT INTO locks(entity_id, name, state, property_id, match_source) "
               "VALUES('lock.a', 'Maple front', 'locked', ?, 'manual')", (pid,))
    m = LockManager(s)
    m.verify_delay = m.gap = 0
    return m


def run(coro):
    return asyncio.run(coro)


def tick_at(m, when):
    m.s.now = lambda: when
    return run(m.tick())


def lock_alerts(m):
    return [t for t, _ in m.ha.notes if t.startswith("Lock problem")]


def lock_row(m):
    return m.db.one("SELECT * FROM locks")


def test_adds_verifies_and_removes_at_checkout(lm):
    lm.s.upsert_reservation(res())
    assert tick_at(lm, at("2030-01-10T08:01")) == 1
    assert lm.ha.codes["lock.a"] == {"Master": "9999", "HA-1": "4821"}
    assert lock_row(lm)["fail_count"] == 0
    assert tick_at(lm, at("2030-01-10T08:30")) == 0  # nothing due until the afternoon check

    lm.ha.delete_times_out = True  # the real lock did this: timed out, still deleted
    assert tick_at(lm, at("2030-01-15T10:01")) == 1
    assert lm.ha.codes["lock.a"] == {"Master": "9999"}
    assert lock_row(lm)["fail_count"] == 0
    kinds = [e["kind"] for e in lm.db.query("SELECT kind FROM events")]
    assert "code.added" in kinds and "code.removed" in kinds


def test_extension_keeps_the_code_and_moves_removal(lm):
    lm.s.upsert_reservation(res())
    tick_at(lm, at("2030-01-10T08:01"))
    lm.s.upsert_reservation(res(departure="2030-01-20"))  # webhook: extended
    assert lock_row(lm)["next_check_at"] is None
    tick_at(lm, at("2030-01-15T10:01"))
    assert "HA-1" in lm.ha.codes["lock.a"]


def test_cancellation_removes_code_right_away(lm):
    lm.s.upsert_reservation(res())
    tick_at(lm, at("2030-01-10T08:01"))
    lm.s.upsert_reservation(res(status="cancelled"))
    tick_at(lm, at("2030-01-10T08:10"))
    assert "HA-1" not in lm.ha.codes["lock.a"]


def test_failed_add_alerts_once_and_retries(lm):
    lm.s.upsert_reservation(res())
    lm.ha.broken_add = {"HA-1"}
    tick_at(lm, at("2030-01-10T08:01"))
    row = lock_row(lm)
    assert row["fail_count"] == 1 and "missing HA-1" in row["last_error"]
    assert row["next_check_at"].startswith("2030-01-10T13:16")  # 15 minutes later, in UTC
    assert len(lock_alerts(lm)) == 1

    tick_at(lm, at("2030-01-10T08:17"))
    assert lock_row(lm)["fail_count"] == 2 and len(lock_alerts(lm)) == 1  # no repeat alert

    lm.ha.broken_add = set()
    tick_at(lm, at("2030-01-10T08:33"))
    assert lock_row(lm)["fail_count"] == 0 and "HA-1" in lm.ha.codes["lock.a"]


def test_final_check_without_backup_alerts_staff_only(lm):
    lm.s.upsert_reservation(res())
    lm.ha.broken_add = {"HA-1"}
    tick_at(lm, at("2030-01-10T08:01"))
    tick_at(lm, at("2030-01-10T15:00"))
    notice = lm.db.one("SELECT * FROM guest_notices")
    assert notice["delivered"] == 0 and lm.hostaway.sent == []
    assert any("NOT confirmed" in title for title, _ in lm.ha.notes)


def test_final_check_sends_backup_code_and_rotates_it_after_checkout(lm):
    lm.db.set_setting("backup_codes_enabled", "1")
    lm.db.set_setting("guest_messages_enabled", "1")
    lm.s.upsert_reservation(res())
    lm.ha.broken_add = {"HA-1"}
    tick_at(lm, at("2030-01-10T08:01"))
    backup = lm.db.one("SELECT backup_code FROM properties")["backup_code"]
    assert lm.ha.codes["lock.a"][BACKUP_NAME] == backup

    tick_at(lm, at("2030-01-10T15:00"))
    tick_at(lm, at("2030-01-10T15:20"))
    assert len(lm.hostaway.sent) == 1  # once per reservation
    rid, body = lm.hostaway.sent[0]
    assert rid == 1 and backup in body and "Ann" in body

    tick_at(lm, at("2030-01-15T10:01"))
    new = lm.db.one("SELECT backup_code, backup_used_by FROM properties")
    assert new["backup_code"] != backup and new["backup_used_by"] is None
    tick_at(lm, at("2030-01-15T10:02"))
    assert lm.ha.codes["lock.a"][BACKUP_NAME] == new["backup_code"]


def test_unreadable_lock_at_final_check_triggers_fallback(lm):
    lm.s.upsert_reservation(res())
    lm.ha.read_fails = True
    tick_at(lm, at("2030-01-10T15:00"))
    assert lock_row(lm)["fail_count"] == 1
    assert lm.db.one("SELECT * FROM guest_notices") is not None


def test_homes_without_automation_are_left_alone(lm):
    lm.db.execute("UPDATE properties SET lock_automation = 0")
    lm.s.upsert_reservation(res())
    assert tick_at(lm, at("2030-01-10T08:01")) == 0
    assert "HA-1" not in lm.ha.codes["lock.a"]


def test_daily_report_once_a_day(lm):
    lm.s.upsert_reservation(res())
    tick_at(lm, at("2030-01-10T06:30"))
    assert not any(t.startswith("Arrivals") for t, _ in lm.ha.notes)
    tick_at(lm, at("2030-01-10T07:05"))
    tick_at(lm, at("2030-01-10T07:10"))
    reports = [m for t, m in lm.ha.notes if t.startswith("Arrivals")]
    assert len(reports) == 1 and "Maple 1" in reports[0] and "1 arrivals today" in reports[0]
