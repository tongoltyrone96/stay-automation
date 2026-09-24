# Stay Automation

Home Assistant add-on that keeps Schlage lock codes and Honeywell thermostats in step with Hostaway reservations.

## Status

Phase 1 (this code): Hostaway sync and webhooks, property and lock tables with automatic lock matching,
dashboard (Status, Properties, Log, Setup), read-only lock code checks for arrivals in the next 36 hours.
Lock writes (phase 2) and thermostats (phase 3) are not built yet.

## Run locally

```sh
pip install -r requirements-dev.txt
python -m pytest
```

Create `.env` in the repo root (never committed):

```
HOSTAWAY_ACCOUNT_ID=...
HOSTAWAY_API_KEY=...
HA_URL=https://your-home-assistant.example.com
```

Put a Home Assistant long-lived access token in `.ha_token`, then:

```sh
PYTHONPATH=stay_automation python -m app.main
```

Open http://127.0.0.1:8099. The local database is `data/stay.db`.

## Install on Home Assistant Green

1. Copy the `stay_automation` folder into the `/addons` share on the Green (Samba share or SSH add-on).
2. Settings → Add-ons → Add-on Store → ⋮ → Check for updates. "Stay Automation" appears under Local add-ons.
3. Install, then set `hostaway_account_id` and `hostaway_api_key` on the Configuration tab.
4. Start it and turn on "Show in sidebar". The dashboard is the "Stays" tab.
5. On the Setup page, save the webhook. Tick "Also register the webhook with Hostaway" once you are ready
   for Hostaway to start sending changes.
