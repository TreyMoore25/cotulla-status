# Cotulla Education — System Status Page

**Live site:** https://cotulla-uptime.azurewebsites.net  
**Last updated:** April 23, 2026

---

## Overview

Flask-based uptime and status monitoring dashboard for Cotulla Education internal and student-facing services. Deployed on Azure App Service (B1 plan) with APScheduler for background health checks, SQLite for ping history, and Slack webhooks for alerting.

---

## Services Monitored

### Internal Systems
| Service | Endpoint |
|---|---|
| Optimus | `https://optimus-cotulla.azurewebsites.net/health` |
| iMAP | `https://imap-app-1748879902.azurewebsites.net` |
| iMAP — Web UI Health | `/health/status` |
| iMAP — API Health | `/api/health/status` |
| iMAP — Blob Storage | parsed from `/api/health/status` → `checks.blob_storage` |
| iMAP — Entra Identity | parsed from `/api/health/status` → `checks.entra` |
| iMAP — Jira Connection | parsed from `/api/health/status` → `checks.jira` |
| iMAP — Snowflake DB | parsed from `/api/health/status` → `checks.snowflake` |

> iMAP sub-components are retrieved in a single HTTP call to `/api/health/status` per cycle — not individual calls.

### Student-Facing Sites
- Aviation Maintenance (aviationmaintenance.edu) — with AIM page drill-down
- Centura College (centuracollege.edu)
- Tidewater Tech Trades (tidewatertechtrades.edu)

### Third-Party Services
- Anthology Student (status.anthology.com) — with sub-components
- Instructure / Canvas (status.instructure.com) — Canvas LMS, Canvas Mobile, Canvas Studio
- Sinch (status.sinch.com) — External Connectivity, Contact Pro, Campaigns, Chatalayer
- Slack (slack-status.com)
- Jira Service Management (jira-service-management.status.atlassian.com)
- Greenhouse (status.greenhouse.io) — with sub-components
- LeadSquared (status.leadsquared.com)

---

## Architecture

### Stack
- **Runtime:** Python 3.10, Flask, Gunicorn (single worker — required for APScheduler)
- **Database:** SQLite via SQLAlchemy (ephemeral on Azure — data resets on restart)
- **Scheduler:** APScheduler `BackgroundScheduler`
- **Auth:** Microsoft Entra ID (MSAL `client_credentials` + authorization code flow)
- **Hosting:** Azure App Service B1 — Canada Central

### Scheduler Jobs
| Job | Interval | What it does |
|---|---|---|
| `run_pings` | Every 5 min | Health checks all endpoints, writes `PingResult` to DB, fires uptime alerts |
| `run_third_party_alerts` | Every 15 min | Checks third-party status APIs, fires Slack alerts on state changes and new incidents |
| `warm_caches` | Every 25 sec | Pre-builds `/api/status` and `/api/hourly-uptime` responses so page loads are instant |

### Data Flow
1. `run_pings` hits each endpoint and writes `PingResult(service, success, pinged_at)` to SQLite
2. Uptime % is calculated on-demand from `PingResult` — 24h and 90-day windows
3. Hourly bars on the live page use the last 24h of `PingResult` grouped by hour
4. Daily bars on the history page use the last 90 days of `PingResult` grouped by calendar day
5. Third-party hourly bars are derived from incident feed timestamps, not ping history

---

## Key Files

```
cotulla-status/
├── app.py                          # Main Flask app — all routes, models, scheduler jobs
├── Procfile                        # gunicorn --workers 1 --timeout 120 --bind 0.0.0.0:8000 app:app
├── requirements.txt                # Python dependencies
├── templates/
│   ├── index.html                  # Live status page
│   └── history.html                # 90-day historical uptime page
├── static/                         # Logos and favicons
├── .azure/config                   # Azure CLI deployment defaults
└── .github/workflows/keepalive.yml # GitHub Actions ping every 5 min (Always On backup)
```

---

## Azure Configuration

### App Service Settings
| Setting | Value |
|---|---|
| Plan | B1 (Basic) |
| Region | Canada Central |
| Always On | Enabled |
| Startup Command | `gunicorn --workers 1 --timeout 120 --bind 0.0.0.0:8000 app:app` |
| SCM_DO_BUILD_DURING_DEPLOYMENT | True |

> **Why single worker?** APScheduler runs in-process. Multiple workers each spin up their own scheduler, causing duplicate jobs, duplicate alerts, and DB conflicts.

> **Why B1?** The free F1 tier does not support Always On. Without Always On the app sleeps during low-traffic periods (overnight), causing data gaps and false downtime.

### Required Environment Variables (Azure Application Settings)
| Variable | Purpose |
|---|---|
| `FLASK_SECRET_KEY` | Session cookie signing |
| `AZURE_TENANT_ID` | Microsoft Entra tenant for admin SSO |
| `AZURE_CLIENT_ID` | App registration client ID |
| `AZURE_CLIENT_SECRET` | App registration client secret |
| `REDIRECT_URI` | OAuth callback — `https://cotulla-uptime.azurewebsites.net/auth/callback` |
| `ADMIN_EMAILS` | Comma-separated list of emails allowed to access admin panel |
| `SLACK_WEBHOOK_URL` | Incoming webhook URL for alert notifications |
| `APPINSIGHTS_APP_ID` | Application Insights app ID (retained for future use) |

---

## Alerting

### Uptime Alerts (Internal Services)
- Fires 🔴 to Slack when any monitored endpoint's 24h uptime drops below **95%**
- Fires ✅ recovery when uptime returns above threshold
- Threshold configurable via `ALERT_THRESHOLD` env var (default: 95.0)

### Third-Party Status Alerts
- **State change alerts:** Fires when Slack, Jira, Greenhouse, Sinch, or Canvas status changes to degraded/outage (including sub-components). Fires recovery on resolution.
- **Incident feed alerts:** Fires when a new incident appears in any third-party status feed (Anthology, LeadSquared, Jira, Greenhouse, Slack, Sinch, Instructure/Canvas). Uses `SeenIncident` DB table to deduplicate — prunes entries older than 7 days.

---

## Admin Panel

Accessible via the 🔒 icon in the footer. Requires Microsoft SSO login with an email in `ADMIN_EMAILS`.

Features:
- Log new incidents (service, severity, description)
- Resolve or delete active incidents
- View and clear backend error log

---

## Sprint Work — April 23, 2026

### Status Page — UI Updates
- Added Snowflake (with logo) to Third-Party Services with status row, hourly bars, and uptime percentage
- Added Checkr (with logo) to Third-Party Services with status row, hourly bars, and uptime percentage
- Reordered Third-Party Services: Anthology → Canvas → Sinch → Slack → Jira → Greenhouse → Snowflake → Checkr → LeadSquared
- Updated "Status sourced from" footer links to include Snowflake and Checkr

### Alerting & Incident Feed
- Added `fetch_snowflake_status()` — polls `status.snowflake.com/api/v2/status.json`
- Added `fetch_checkr_status()` — polls `checkrstatus.com/api/v2/status.json`
- Wired Snowflake and Checkr into `run_third_party_alerts()` for real-time Slack state-change alerts
- Added Snowflake and Checkr to incident feed sources for new incident Slack alerts
- **Fixed active incident display** — unresolved incidents now always appear in the rolling 7-day list regardless of how old they are; only resolved incidents are subject to the 7-day cutoff (previously an unresolved incident older than 7 days, like the Snowflake AWS UAE outage, would be silently excluded)

### Sinch
- Updated `fetch_sinch_status()` to filter US-only components (North America, `- US` keyword matching) for overall status and sub-service mapping
- Sub-services now reflect US-region health only: External Connectivity, Contact Pro, Campaigns, Chatalayer

### Admin Panel
- Fixed login failure caused by Flask session cookie overflow — MSAL auth flow data + full token claims exceeded the 4KB cookie limit
- Fixed by clearing session before starting OAuth flow and storing only essential claims (`preferred_username`, `name`) after login
- Updated service dropdown to full grouped list: Internal / Student-Facing / Third-Party

### Infrastructure
- Bumped `ThreadPoolExecutor` max_workers across all executors to handle additional concurrent fetches (up to 14 in `_build_status`)

---

## Sprint Work — April 21, 2026

### Status Page — UI Updates
- Updated support ticket URL to specific service desk portal (`/portal/302`)
- Removed duplicate "Need help?" ticket link from hero section
- Renamed "Optimus — Student Portal" → "Optimus — Student Management"
- Added service desk help links to Optimus, Student-Facing Sites, Anthology, Sinch, and Instructure (Canvas) rows
- Added Sinch (with sub-services: External Connectivity, Contact Pro, Campaigns, Chatalayer) to Third-Party Services
- Added Instructure (Canvas) (with sub-services: Canvas LMS, Canvas Mobile, Canvas Studio) to Third-Party Services
- Added logos for Sinch and Canvas from static assets
- Reordered Third-Party Services: Anthology → Canvas → Sinch → Slack → Jira → Greenhouse → LeadSquared

### Alerting
- Added `fetch_sinch_status()` — polls `status.sinch.com` components API, maps sub-services by name
- Added `fetch_canvas_status()` — polls `status.instructure.com` components API, maps Canvas LMS/Mobile/Studio
- Both wired into `run_third_party_alerts()` for real-time state-change Slack alerts
- Added Sinch and Instructure to incident feed sources (`sp_sources`) for new incident alerts
- Bumped `ThreadPoolExecutor` from 5 → 8 workers to handle additional concurrent status fetches

---

## Sprint Work — April 16, 2026

The following changes were made and deployed this sprint:

### Optimus
- Removed auth-protected sub-endpoints (`optimus_api`, `optimus_student`, `optimus_career`, `optimus_partner`, `optimus_infra`) — these were hitting authenticated URLs and recording false failures
- Monitoring now uses only the legitimate `/health` endpoint provided by the Optimus team
- Removed drill-down sub-rows from both live and history pages
- Switched from Application Insights API (deprecated March 2026) to DB-based ping history for all uptime calculations

### iMAP
- Verified all 6 sub-component health checks against live API response
- Confirmed JSON keys (`blob_storage`, `entra`, `jira`, `snowflake`) match monitoring configuration
- All 6 sub-components tracked independently with hourly uptime bars on live page
- Live response times confirmed: Blob 41ms, Entra 99ms, Jira 194ms, Snowflake 469ms

### Infrastructure
- Upgraded Azure App Service from **F1 → B1** to enable Always On
- Enabled **Always On** — app no longer sleeps overnight, eliminating data gaps
- Added **GitHub Actions keepalive** workflow (`.github/workflows/keepalive.yml`) — pings status page every 5 minutes as secondary keep-alive
- Fixed Gunicorn startup — Azure was ignoring Procfile and spawning multiple workers; startup command set directly in Azure Portal
- Added `SLACK_WEBHOOK_URL` to Azure Application Settings — Slack alerts now live

### Status Page
- Renamed "Customer-Facing Sites" → "Student-Facing Sites" on both pages
- Fixed hourly bars showing no data after business hours (UTC/local timezone mismatch in JS)
- Increased uptime precision from 2 to 3 decimal places for internal services
- Added admin error log panel (view and clear backend errors from admin panel)
- Fixed `buildBar` double-render bug on history page (missing `innerHTML = ''` clear)
- Fixed iMAP main bar on history page not pulling from daily uptime API
- Fixed third-party incident Slack alerts not firing for resolved incidents

---

## Known Limitations / Future Work

- **SQLite is ephemeral** — all ping history and alert state is lost on app restart. Consider migrating to Azure SQL or PostgreSQL for persistence.
- **Maintenance windows** — Optimus and iMAP have scheduled overnight sync windows that cause legitimate downtime. A maintenance window feature (to exclude planned downtime from SLA calculations) is planned but not yet implemented. Required for accurate tracking toward 99.999% uptime.
- **LeadSquared real-time alerts** — LeadSquared is covered by the incident feed alert but not the real-time service state check. If it goes degraded between incidents, no immediate alert fires.
- **iMAP sub-component daily history** — Sub-components (blob, entra, jira, snowflake) are tracked hourly on the live page but not yet in the 90-day daily history view.
- **Response time history** — Current `PingResult` model only stores `success: bool`. Adding `response_ms` would enable latency trending for both iMAP latency and Optimus Road to 5 9s tickets.
