from datetime import datetime
from zoneinfo import ZoneInfo

from app.reservations import changes, code_status, normalize, property_state

TZ = ZoneInfo("America/New_York")


def raw(**kw):
    base = {
        "id": 1, "listingMapId": 10, "status": "new", "guestName": "Ann Lee",
        "arrivalDate": "2026-10-01", "departureDate": "2026-10-05",
        "checkInTime": 16, "checkOutTime": 10, "doorCode": "4821",
    }
    return {**base, **kw}


def norm(**kw):
    return normalize(raw(**kw), TZ, 15, 10)


def test_normalize_uses_reservation_hours_and_timezone():
    r = norm()
    assert r["check_in_at"] == "2026-10-01T16:00:00-04:00"
    assert r["check_out_at"] == "2026-10-05T10:00:00-04:00"
    assert r["active"] == 1 and r["door_code"] == "4821"


def test_normalize_falls_back_to_default_hours_and_handles_dst():
    r = norm(checkInTime=None, checkOutTime="", departureDate="2026-11-03")
    assert r["check_in_at"].startswith("2026-10-01T15:00")
    assert r["check_out_at"] == "2026-11-03T10:00:00-05:00"


def test_inactive_statuses_and_blank_code():
    assert norm(status="cancelled")["active"] == 0
    assert norm(status="inquiry")["active"] == 0
    assert norm(status="ownerStay")["active"] == 1
    assert norm(doorCode="  ")["door_code"] is None


def test_changes():
    old = norm()
    assert changes(None, old) == ["created"]
    assert changes(None, norm(status="inquiry")) == []
    assert changes(old, norm(departureDate="2026-10-08")) == ["extended"]
    assert changes(old, norm(departureDate="2026-10-03")) == ["shortened"]
    assert changes(old, norm(status="cancelled")) == ["cancelled"]
    assert changes(norm(status="cancelled"), old) == ["reinstated"]
    assert changes(old, norm(doorCode="1111")) == ["code_changed"]
    assert changes(norm(doorCode=None), old) == ["code_set"]
    assert changes(old, norm(arrivalDate="2026-09-30")) == ["arrival_changed"]
    assert changes(old, norm()) == []


def at(s):
    return datetime.fromisoformat(s).replace(tzinfo=TZ)


def test_property_state():
    stay1 = norm(id=1, arrivalDate="2026-10-01", departureDate="2026-10-05")
    stay2 = norm(id=2, arrivalDate="2026-10-05", departureDate="2026-10-09")
    gone = norm(id=3, arrivalDate="2026-10-02", departureDate="2026-10-04", status="cancelled")
    rs = [stay2, gone, stay1]

    s = property_state(rs, at("2026-10-03T12:00"))
    assert s.status == "occupied" and s.current["id"] == 1 and s.next["id"] == 2

    assert property_state(rs, at("2026-10-05T08:00")).status == "turnover_today"
    assert property_state(rs, at("2026-10-05T12:00")).status == "arriving_today"
    assert property_state(rs, at("2026-10-09T08:00")).status == "departing_today"

    s = property_state(rs, at("2026-10-10T08:00"))
    assert s.status == "vacant" and s.current is None and s.next is None


def test_code_status():
    h = lambda c: "h" + c  # noqa: E731
    r = norm()
    assert code_status(None, None, True, h) == "none"
    assert code_status(r, None, False, h) == "no_lock"
    assert code_status(norm(doorCode=None), set(), True, h) == "no_code"
    assert code_status(r, None, True, h) == "unknown"
    assert code_status(r, {"h4821"}, True, h) == "in_lock"
    assert code_status(r, {"h1111"}, True, h) == "missing"
