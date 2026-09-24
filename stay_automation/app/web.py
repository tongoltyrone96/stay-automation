"""Dashboard served through HA Ingress. All links are relative so they work under the ingress path."""
import json
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .reservations import code_status, property_state, within
from .sync import Syncer

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")

STATUS_LABELS = {
    "occupied": "Occupied",
    "vacant": "Vacant",
    "arriving_today": "Arriving today",
    "departing_today": "Departing today",
    "turnover_today": "Turnover today",
}
CODE_LABELS = {
    "in_lock": "In lock",
    "missing": "NOT in lock",
    "unknown": "Not checked yet",
    "no_code": "No code from Hostaway",
    "no_lock": "No lock",
    "none": "",
}


def _fmt(iso: str | None, tz) -> str:
    if not iso:
        return ""
    return datetime.fromisoformat(iso).astimezone(tz).strftime("%a %b %d, %I:%M %p").replace(" 0", " ")


def dashboard_rows(syncer: Syncer, query: str = "") -> list[dict[str, Any]]:
    now = syncer.now()
    tz = syncer.tz
    locks_by_property: dict[int, list[dict]] = {}
    for lock in syncer.db.query("SELECT * FROM locks WHERE property_id IS NOT NULL ORDER BY name"):
        locks_by_property.setdefault(lock["property_id"], []).append(lock)

    rows = []
    for prop in syncer.db.query("SELECT * FROM properties WHERE active = 1 ORDER BY name"):
        if query and query.lower() not in prop["name"].lower():
            continue
        state = property_state(syncer.reservations_for(prop["id"]), now)
        locks = locks_by_property.get(prop["id"], [])
        per_lock = [
            code_status(
                state.next, None if lock["code_hashes"] is None else set(json.loads(lock["code_hashes"])),
                True, syncer.code_hash,
            )
            for lock in locks
        ] or [code_status(state.next, None, False, syncer.code_hash)]
        # The worst lock decides: one lock without the code means the guest may be stuck at that door.
        order = ["missing", "unknown", "no_code", "in_lock", "no_lock", "none"]
        code = min(per_lock, key=order.index)

        problems = []
        soon = within(state.next, now, 36)
        if code == "missing" and soon:
            problems.append("Guest code not in lock")
        if code == "no_code" and soon:
            problems.append("No door code in Hostaway")
        for lock in locks:
            if lock["state"] == "unavailable":
                problems.append(f"{lock['name']} offline")
            elif lock["codes_error"]:
                problems.append(f"{lock['name']}: read failed")

        rows.append({
            "id": prop["id"],
            "name": prop["name"],
            "status": state.status,
            "status_label": STATUS_LABELS[state.status],
            "current_guest": state.current["guest_name"] if state.current else "",
            "current_out": _fmt(state.current["check_out_at"], tz) if state.current else "",
            "next_guest": state.next["guest_name"] if state.next else "",
            "next_in": _fmt(state.next["check_in_at"], tz) if state.next else "",
            "next_in_sort": state.next["check_in_at"] if state.next else "9999",
            "code": code,
            "code_label": CODE_LABELS[code],
            "locks": locks,
            "lock_read": _fmt(max((l["codes_read_at"] for l in locks if l["codes_read_at"]), default=None), tz),
            "problems": problems,
            "lock_automation": prop["lock_automation"],
        })
    rows.sort(key=lambda r: (not r["problems"], r["next_in_sort"]))
    return rows


def create_app(syncer: Syncer, lifespan=None) -> FastAPI:
    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    db = syncer.db

    def page(request: Request, name: str, **ctx: Any) -> HTMLResponse:
        return templates.TemplateResponse(request, name, {
            "last_sync": _fmt(db.get_setting("last_sync_at"), syncer.tz),
            "last_webhook": _fmt(db.get_setting("last_webhook_at"), syncer.tz),
            **ctx,
        })

    @app.get("/", response_class=HTMLResponse)
    async def status(request: Request, q: str = ""):
        rows = dashboard_rows(syncer, q)
        counts = {k: sum(1 for r in rows if r["status"] == k) for k in STATUS_LABELS}
        return page(request, "status.html", rows=rows, q=q, counts=counts,
                    problems=sum(1 for r in rows if r["problems"]))

    @app.get("/properties", response_class=HTMLResponse)
    async def properties(request: Request):
        props = db.query("SELECT * FROM properties ORDER BY name")
        locks = db.query("SELECT * FROM locks ORDER BY name")
        return page(request, "properties.html", properties=props, locks=locks)

    @app.post("/properties-save")
    async def properties_save(request: Request):
        form = await request.form()
        for prop in db.query("SELECT id FROM properties"):
            pid = prop["id"]
            name = str(form.get(f"name_{pid}", "")).strip()
            if name:
                db.execute(
                    "UPDATE properties SET name = ?, active = ?, lock_automation = ? WHERE id = ?",
                    (name, 1 if form.get(f"active_{pid}") else 0,
                     1 if form.get(f"auto_{pid}") else 0, pid),
                )
        for lock in db.query("SELECT id, property_id FROM locks"):
            value = str(form.get(f"lock_{lock['id']}", ""))
            new_pid = int(value) if value.isdigit() else None
            if new_pid != lock["property_id"]:
                syncer.assign_lock(lock["id"], new_pid)
        db.log("properties.saved", "Property settings saved")
        return RedirectResponse("properties", status_code=303)

    @app.get("/events", response_class=HTMLResponse)
    async def events(request: Request):
        rows = db.query(
            "SELECT e.*, p.name AS property FROM events e LEFT JOIN properties p ON p.id = e.property_id "
            "ORDER BY e.id DESC LIMIT 300"
        )
        for r in rows:
            r["at_local"] = _fmt(r["at"], syncer.tz)
        return page(request, "events.html", events=rows)

    @app.post("/sync")
    async def sync_now():
        await syncer.import_listings()
        await syncer.discover_locks()
        changed = await syncer.sync_reservations()
        db.log("sync.manual", f"Manual sync: {changed} reservations changed")
        return RedirectResponse(".", status_code=303)

    @app.post("/lock-refresh")
    async def lock_refresh(property_id: int = Form(...)):
        for lock in db.query("SELECT * FROM locks WHERE property_id = ?", (property_id,)):
            await syncer.read_lock_codes(lock)
        return RedirectResponse(".", status_code=303)

    @app.get("/setup", response_class=HTMLResponse)
    async def setup(request: Request):
        webhook_id = db.get_setting("webhook_id")
        public_url = db.get_setting("public_url", "")
        try:
            ha_ok = await syncer.ha.ping()
            automation = await syncer.ha.webhook_automation_exists()
        except Exception:
            ha_ok, automation = False, False
        return page(
            request, "setup.html",
            ha_ok=ha_ok, automation=automation, public_url=public_url,
            addon_slug=db.get_setting("addon_slug", "local_stay_automation"),
            webhook_url=f"{public_url}/api/webhook/{webhook_id[:6]}…" if webhook_id else "",
            registered=db.get_setting("hostaway_webhook_registered_at"),
            is_addon=syncer.settings.is_addon,
        )

    @app.post("/setup-webhook")
    async def setup_webhook(public_url: str = Form(...), addon_slug: str = Form(...),
                            register_hostaway: str = Form("")):
        public_url = public_url.strip().rstrip("/")
        if not public_url.startswith("https://"):
            raise HTTPException(400, "Public URL must start with https://")
        db.set_setting("public_url", public_url)
        db.set_setting("addon_slug", addon_slug.strip())
        webhook_id = db.get_setting("webhook_id") or secrets.token_urlsafe(32)
        db.set_setting("webhook_id", webhook_id)
        await syncer.ha.create_webhook_automation(webhook_id, addon_slug.strip())
        db.log("setup.webhook", "Home Assistant webhook automation saved")
        if register_hostaway:
            await syncer.hostaway.register_webhook(f"{public_url}/api/webhook/{webhook_id}")
            db.set_setting("hostaway_webhook_registered_at", datetime.now(syncer.tz).isoformat())
            db.log("setup.webhook", "Webhook registered with Hostaway")
        return RedirectResponse("setup", status_code=303)

    @app.post("/webhook/{secret}")
    async def webhook_direct(secret: str, request: Request):
        """Direct webhook for local development; on HA the webhook arrives through stdin."""
        expected = db.get_setting("webhook_id")
        if not expected or not secrets.compare_digest(secret, expected):
            raise HTTPException(404)
        what = await syncer.handle_webhook(await request.json())
        return {"changes": what}

    @app.get("/health")
    async def health():
        return {"ok": True}

    return app
