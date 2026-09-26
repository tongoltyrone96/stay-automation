"""Pure reservation logic: normalize Hostaway records, detect changes, compute property status."""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

ACTIVE_STATUSES = {"new", "modified", "ownerStay"}


def _hour(value: Any, default: int) -> int:
    try:
        hour = int(value)
    except (TypeError, ValueError):
        return default
    return hour if 0 <= hour <= 23 else default


def normalize(raw: dict[str, Any], tz: ZoneInfo, checkin_hour: int, checkout_hour: int) -> dict[str, Any]:
    arrival = date.fromisoformat(raw["arrivalDate"])
    departure = date.fromisoformat(raw["departureDate"])
    check_in = datetime.combine(arrival, time(_hour(raw.get("checkInTime"), checkin_hour)), tz)
    check_out = datetime.combine(departure, time(_hour(raw.get("checkOutTime"), checkout_hour)), tz)
    code = str(raw.get("doorCode") or "").strip() or None
    return {
        "id": int(raw["id"]),
        "listing_id": int(raw["listingMapId"]),
        "guest_name": raw.get("guestName") or raw.get("guestFirstName"),
        "status": raw.get("status") or "unknown",
        "active": 1 if raw.get("status") in ACTIVE_STATUSES else 0,
        "arrival_date": arrival.isoformat(),
        "departure_date": departure.isoformat(),
        "check_in_at": check_in.isoformat(),
        "check_out_at": check_out.isoformat(),
        "door_code": code,
    }


def changes(old: dict[str, Any] | None, new: dict[str, Any]) -> list[str]:
    """What happened to a reservation, in the words the lock/thermostat logic cares about."""
    if old is None:
        return ["created"] if new["active"] else []
    if old["active"] and not new["active"]:
        return ["cancelled"]
    if not old["active"] and new["active"]:
        return ["reinstated"]
    if not new["active"]:
        return []
    out = []
    if new["listing_id"] != old["listing_id"]:
        out.append("moved")
    if new["check_in_at"] != old["check_in_at"]:
        out.append("arrival_changed")
    old_out = datetime.fromisoformat(old["check_out_at"])
    new_out = datetime.fromisoformat(new["check_out_at"])
    if new_out > old_out:
        out.append("extended")
    elif new_out < old_out:
        out.append("shortened")
    if new["door_code"] != old["door_code"]:
        out.append("code_set" if old["door_code"] is None else "code_changed")
    return out


@dataclass
class PropertyState:
    status: str  # occupied | vacant | arriving_today | departing_today | turnover_today
    current: dict[str, Any] | None
    next: dict[str, Any] | None


def property_state(reservations: list[dict[str, Any]], now: datetime) -> PropertyState:
    def at(r: dict[str, Any], key: str) -> datetime:
        return datetime.fromisoformat(r[key])

    active = sorted((r for r in reservations if r["active"]), key=lambda r: at(r, "check_in_at"))
    current = next((r for r in active if at(r, "check_in_at") <= now < at(r, "check_out_at")), None)
    upcoming = next((r for r in active if at(r, "check_in_at") > now), None)
    today = now.date().isoformat()
    departing = current is not None and current["departure_date"] == today
    arriving = upcoming is not None and upcoming["arrival_date"] == today
    if departing and arriving:
        status = "turnover_today"
    elif departing:
        status = "departing_today"
    elif arriving:
        status = "arriving_today"
    elif current:
        status = "occupied"
    else:
        status = "vacant"
    return PropertyState(status, current, upcoming)


def code_status(reservation: dict[str, Any] | None, lock_code_hashes: set[str] | None,
                has_lock: bool, code_hash) -> str:
    """Is this guest's code in the lock? in_lock | missing | unknown | no_code | no_lock | none."""
    if reservation is None:
        return "none"
    if not has_lock:
        return "no_lock"
    if not reservation["door_code"]:
        return "no_code"
    if lock_code_hashes is None:
        return "unknown"
    return "in_lock" if code_hash(reservation["door_code"]) in lock_code_hashes else "missing"


def within(reservation: dict[str, Any] | None, now: datetime, hours: int) -> bool:
    if reservation is None:
        return False
    return datetime.fromisoformat(reservation["check_in_at"]) <= now + timedelta(hours=hours)
