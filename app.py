from flask import Flask, render_template, jsonify, session, redirect, url_for, request
from flask_sqlalchemy import SQLAlchemy
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
import urllib.request
import urllib.parse
import urllib.error
import msal
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

load_dotenv(os.path.expanduser("~/Projects/env/.env.status"), override=False)

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-key")
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///incidents.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["2000 per day", "200 per hour"],
    storage_uri="memory://",
)

# --- Entra config ---
TENANT_ID = os.getenv("AZURE_TENANT_ID")
CLIENT_ID = os.getenv("AZURE_CLIENT_ID")
CLIENT_SECRET = os.getenv("AZURE_CLIENT_SECRET")
AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
REDIRECT_URI = os.getenv("REDIRECT_URI", "http://localhost:5000/auth/callback")
SCOPE = ["User.Read"]

# --- Authorized admins ---
AUTHORIZED_ADMINS = [
    e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()
]

# --- Application Insights config ---
APPINSIGHTS_APP_ID = os.getenv("APPINSIGHTS_APP_ID")

# --- Alert config ---
ALERT_THRESHOLD = float(os.getenv("ALERT_THRESHOLD", "95.0"))
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")
TEAMS_WEBHOOK_URL = os.getenv("TEAMS_WEBHOOK_URL", "")
SYNC_API_KEY        = os.getenv("SYNC_API_KEY", "")
STATUS_PAGE_URL     = os.getenv("STATUS_PAGE_URL", "")
SLOW_RESPONSE_MS    = int(os.getenv("SLOW_RESPONSE_MS", "5000"))    # alert threshold

# --- Endpoints to monitor ---
BASE = "https://optimus-cotulla.azurewebsites.net"
MONITORED_ENDPOINTS = {
    "optimus": f"{BASE}/health",
    "imap":             "https://imap-app-1748879902.azurewebsites.net",
    "imap_web_health":  "https://imap-app-1748879902.azurewebsites.net/health/status",
    "imap_api_health":  "https://imap-app-1748879902.azurewebsites.net/api/health/status",
    "imap_blob":        "https://imap-app-1748879902.azurewebsites.net/api/health/status",
    "imap_entra":       "https://imap-app-1748879902.azurewebsites.net/api/health/status",
    "imap_jira_conn":   "https://imap-app-1748879902.azurewebsites.net/api/health/status",
    "imap_snowflake":   "https://imap-app-1748879902.azurewebsites.net/api/health/status",
    "school_aviation":  "https://aviationmaintenance.edu/",
    "school_centura":   "https://www.centuracollege.edu/",
    "school_tidewater": "https://tidewatertechtrades.edu/",
    # AIM content-aware page checks
    "aim_admissions": "https://aviationmaintenance.edu/admissions/",
    "aim_campuses":   "https://aviationmaintenance.edu/campuses/",
    "aim_programs":   "https://aviationmaintenance.edu/programs/aviation/",
    "aim_resources":  "https://aviationmaintenance.edu/your-rights-old/student-resources/",
    "aim_about":      "https://aviationmaintenance.edu/about/",
}

# Keywords that must appear in each AIM page response (soft-fail detection)
IMAP_COMPONENT_KEYS = {
    "imap_web_health", "imap_api_health",
    "imap_blob", "imap_entra", "imap_jira_conn", "imap_snowflake",
}

CONTENT_PAGE_CHECKS = {
    "aim_admissions": ["admissions", "apply"],
    "aim_campuses":   ["campus"],
    "aim_programs":   ["aviation"],
    "aim_resources":  ["resources"],
    "aim_about":      ["about aim", "about"],
}


# --- Models ---
class Incident(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    service = db.Column(db.String(64), nullable=False)
    severity = db.Column(db.String(32), nullable=False)
    description = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(16), default="active")
    started_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    resolved_at = db.Column(db.DateTime, nullable=True)


class PingResult(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    service = db.Column(db.String(64), nullable=False)
    success = db.Column(db.Boolean, nullable=False)
    pinged_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


class AlertState(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    service = db.Column(db.String(64), unique=True, nullable=False)
    in_alert = db.Column(db.Boolean, default=False)
    last_pct = db.Column(db.Float, nullable=True)
    alerted_at = db.Column(db.DateTime, nullable=True)


class SeenIncident(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    incident_id = db.Column(db.String(256), unique=True, nullable=False)
    seen_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


class ErrorLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    logged_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    source = db.Column(db.String(64), nullable=False)
    message = db.Column(db.Text, nullable=False)


with app.app_context():
    db.create_all()


def _log_error(source, message):
    """Log an error to stdout and persist to DB for admin review."""
    from flask import has_app_context
    print(f"[{source}] {message}")
    try:
        if has_app_context():
            db.session.add(ErrorLog(source=source, message=str(message)))
            db.session.commit()
        else:
            with app.app_context():
                db.session.add(ErrorLog(source=source, message=str(message)))
                db.session.commit()
    except Exception:
        pass


# --- Simple in-memory TTL cache ---
_cache: dict = {}
# Latest response-time metrics from ping job (resets on restart — intentional)
_latest_metrics: dict = {}  # service -> {"response_ms": int}

def _cached(key, ttl, fn):
    """Return cached result if fresh, otherwise call fn() and cache it."""
    entry = _cache.get(key)
    if entry is not None and time.time() - entry["ts"] < ttl:
        return entry["data"]
    data = fn()
    _cache[key] = {"ts": time.time(), "data": data}
    return data


# --- Slack notification helpers ---
_STATUS_EMOJI = {
    "operational": "✅",
    "degraded":    "⚠️",
    "partial":     "🟠",
    "outage":      "🔴",
    "unknown":     "⚫",
}

_MONITORED_LABELS = {
    "optimus":          "Optimus",
    "imap":             "iMAP",
    "imap_web_health":  "iMAP — Web UI",
    "imap_api_health":  "iMAP — API",
    "imap_blob":        "iMAP — Blob Storage",
    "imap_entra":       "iMAP — Entra Identity",
    "imap_jira_conn":   "iMAP — Jira Connection",
    "imap_snowflake":   "iMAP — Snowflake DB",
    "school_aviation":  "Aviation Maintenance",
    "school_centura":   "Centura College",
    "school_tidewater": "Tidewater Tech Trades",
    "aim_admissions":   "AIM — Get Started (Admissions)",
    "aim_campuses":     "AIM — Campuses",
    "aim_programs":     "AIM — Programs",
    "aim_resources":    "AIM — Student Resources",
    "aim_about":        "AIM — About",
}

_TP_LABELS = {
    "tp_slack":                 "Slack",
    "tp_jira":                  "Jira Service Management",
    "tp_greenhouse_website":    "Greenhouse (Website)",
    "tp_greenhouse_recruiting": "Greenhouse (Recruiting)",
    "tp_greenhouse_harvest":    "Greenhouse (Harvest API)",
    "tp_greenhouse_jobboards":  "Greenhouse (Job Boards)",
}



def _status_page_url():
    if STATUS_PAGE_URL:
        return STATUS_PAGE_URL
    return os.getenv("REDIRECT_URI", "http://localhost:5000").rsplit("/auth", 1)[0]


def _post_slack(text):
    """Post a plain-text / mrkdwn message to the Slack webhook."""
    if not SLACK_WEBHOOK_URL:
        return
    try:
        payload = json.dumps({"text": text}).encode()
        req = urllib.request.Request(
            SLACK_WEBHOOK_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        _log_error("slack_post", e)


def _get_incidents_24h():
    """Return internal + third-party incidents started or still active in the last 24h."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    results = []

    # Internal incidents from DB
    internal = Incident.query.filter(
        (Incident.status == "active") |
        ((Incident.status == "resolved") & (Incident.started_at >= cutoff))
    ).order_by(Incident.started_at.desc()).all()
    for i in internal:
        results.append({
            "source":     "internal",
            "service":    i.service,
            "title":      i.description,
            "status":     i.status,
            "severity":   i.severity,
            "started_at": i.started_at,
        })

    # Third-party incidents (use cache if warm, otherwise skip to avoid slowing digest)
    tp_cached = _cache.get("third_party_incidents")
    if tp_cached:
        for inc in tp_cached["data"]:
            try:
                started = datetime.fromisoformat(
                    inc.get("sort_key") or inc.get("started_at", "")
                ).replace(tzinfo=timezone.utc) if inc.get("sort_key") else None
            except Exception:
                started = None
            if started and started >= cutoff:
                results.append({
                    "source":     "third_party",
                    "service":    inc["service"],
                    "title":      inc["title"],
                    "status":     inc["status"],
                    "severity":   inc.get("impact", "minor"),
                    "started_at": started,
                })

    return results




def run_third_party_alerts():
    """Check third-party services and send Slack alerts on state changes."""
    with app.app_context():
        checks = {}
        try:
            checks["tp_slack"] = fetch_slack_status()
        except Exception:
            pass
        try:
            checks["tp_jira"] = fetch_jira_status()
        except Exception:
            pass
        try:
            for sub, status in fetch_greenhouse_status().items():
                checks[f"tp_{sub}"] = status
        except Exception:
            pass

        for key, result in checks.items():
            is_down = result["status"] not in ("operational", "unknown")
            state = AlertState.query.filter_by(service=key).first()
            if state is None:
                state = AlertState(service=key, in_alert=False)
                db.session.add(state)
                db.session.flush()

            label = _TP_LABELS.get(key, key.replace("tp_", "").replace("_", " ").title())
            url   = _status_page_url()
            if is_down and not state.in_alert:
                state.in_alert  = True
                state.alerted_at = datetime.now(timezone.utc)
                _post_slack(
                    f"🔴 *{label}* is reporting issues: *{result['label']}*\n"
                    f"_<{url}|View status page>_"
                )
            elif not is_down and state.in_alert:
                state.in_alert = False
                _post_slack(f"✅ *{label}* has recovered: *{result['label']}*")

        db.session.commit()

        # Check third-party incident feeds for new incidents (active or recently resolved)
        inc_cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        sp_sources = [
            ("Anthology",               "https://status.anthology.com/api/v2/incidents.json"),
            ("LeadSquared",             "https://status.leadsquared.com/api/v2/incidents.json"),
            ("Jira Service Management", "https://jira-service-management.status.atlassian.com/api/v2/incidents.json"),
            ("Greenhouse",              "https://status.greenhouse.io/api/v2/incidents.json"),
        ]
        try:
            with ThreadPoolExecutor(max_workers=5) as ex:
                f_slk = ex.submit(_fetch_slack_incidents, inc_cutoff)
                sp_fts = [ex.submit(_fetch_statuspage_incidents, name, u, inc_cutoff) for name, u in sp_sources]
            inc_results = f_slk.result()
            for f in sp_fts:
                inc_results.extend(f.result())
            sev_emoji = {"outage": "🔴", "major": "🔴", "minor": "⚠️", "none": "✅", "critical": "🔴"}
            for inc in inc_results:
                inc_id = f"{inc['service']}:{inc.get('sort_key', inc.get('started_at', ''))}"
                already_seen = SeenIncident.query.filter_by(incident_id=inc_id).first()
                if not already_seen:
                    db.session.add(SeenIncident(incident_id=inc_id))
                    emoji = sev_emoji.get(inc.get("impact", "minor"), "⚠️")
                    status_note = " _(since resolved)_" if inc.get("status") == "resolved" else ""
                    _post_slack(
                        f"{emoji} *Third-party incident — {inc['service']}*{status_note}\n"
                        f"_{inc['title']}_\n"
                        f"_Started: {inc['started_at']} — <{url}|view status page>_"
                    )
            # Prune seen incidents older than 7 days
            prune_cutoff = datetime.now(timezone.utc) - timedelta(days=7)
            SeenIncident.query.filter(SeenIncident.seen_at < prune_cutoff).delete()
            db.session.commit()
        except Exception as e:
            _log_error("tp_incident_alert", e)


def get_latest_ping_status(service):
    """Return status from last DB ping result — no live HTTP call."""
    recent = (
        PingResult.query
        .filter_by(service=service)
        .order_by(PingResult.pinged_at.desc())
        .limit(3).all()
    )
    if not recent:
        return {"status": "unknown", "label": "Unknown"}
    if recent[0].success:
        return {"status": "operational", "label": "Operational"}
    fails = sum(1 for r in recent if not r.success)
    return (
        {"status": "outage", "label": "Major Outage"}
        if fails >= 2
        else {"status": "degraded", "label": "Degraded"}
    )


# --- Security headers ---
@app.after_request
def add_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https://a.slack-edge.com; "
        "connect-src 'self'; "
        "frame-ancestors 'none';"
    )
    return response


# --- Health check ---
def check_app_health(url, timeout=10):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return (
                {"status": "operational", "label": "Operational"}
                if r.status == 200
                else {"status": "degraded", "label": "Degraded"}
            )
    except urllib.error.HTTPError as e:
        if e.code in [401, 403]:
            return {"status": "operational", "label": "Operational"}
        return {"status": "degraded", "label": "Degraded"}
    except Exception as e:
        _log_error("health_check", f"{url}: {e}")
        return {"status": "outage", "label": "Major Outage"}


def check_page_content(url, keywords, timeout=10):
    """GET url, confirm 200, check for keyword presence, and measure response time."""
    start = time.time()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            response_ms = int((time.time() - start) * 1000)
            if r.status != 200:
                return {"status": "degraded", "label": f"HTTP {r.status}", "response_ms": response_ms}
            body = r.read(50 * 1024).decode("utf-8", errors="ignore").lower()
            if keywords and not any(kw.lower() in body for kw in keywords):
                return {"status": "degraded", "label": "Content missing", "response_ms": response_ms}
            if response_ms > SLOW_RESPONSE_MS:
                return {"status": "degraded", "label": f"Slow ({response_ms:,}ms)", "response_ms": response_ms}
            return {"status": "operational", "label": f"Operational ({response_ms:,}ms)", "response_ms": response_ms}
    except urllib.error.HTTPError as e:
        response_ms = int((time.time() - start) * 1000)
        if e.code in [401, 403]:
            return {"status": "operational", "label": f"Operational ({response_ms:,}ms)", "response_ms": response_ms}
        return {"status": "degraded", "label": f"HTTP {e.code}", "response_ms": response_ms}
    except Exception as e:
        response_ms = int((time.time() - start) * 1000)
        _log_error("content_check", f"{url}: {e}")
        return {"status": "outage", "label": "Major Outage", "response_ms": response_ms}


def check_optimus_health():
    """Ping /health and confirm JSON {"status":"ok"}."""
    try:
        req = urllib.request.Request(
            f"{BASE}/health", headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        if data.get("status") == "ok":
            return {"status": "operational", "label": "Operational"}
        return {"status": "degraded", "label": "Degraded"}
    except Exception as e:
        _log_error("optimus_health", e)
        return {"status": "outage", "label": "Major Outage"}


def check_optimus_sync():
    """Check career-services sync status — requires SYNC_API_KEY."""
    if not SYNC_API_KEY:
        return {"status": "unknown", "label": "Unknown"}
    try:
        req = urllib.request.Request(
            f"{BASE}/api/career-services/sync",
            headers={"User-Agent": "Mozilla/5.0", "x-sync-api-key": SYNC_API_KEY},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        last_sync = data.get("lastSync", {})
        sync_status = last_sync.get("status", "")
        hours_since = data.get("hoursSinceLastSync", 999)
        if sync_status == "completed" and hours_since < 24:
            return {"status": "operational", "label": "Operational"}
        if hours_since >= 24:
            return {"status": "degraded", "label": "Sync Overdue"}
        return {"status": "degraded", "label": "Degraded"}
    except Exception as e:
        _log_error("optimus_sync", e)
        return {"status": "outage", "label": "Major Outage"}


def check_imap_components():
    """Single call to /api/health/status — returns statuses for all 6 iMAP keys."""
    IMAP_BASE = "https://imap-app-1748879902.azurewebsites.net"
    results = {}

    def _status_from(raw):
        if raw == "operational":   return "operational"
        if raw == "healthy":       return "operational"
        if raw in ("degraded", "degraded_performance", "partial_outage"): return "degraded"
        if raw == "major_outage":  return "outage"
        return "unknown"

    # Web UI health
    try:
        start = time.time()
        req = urllib.request.Request(f"{IMAP_BASE}/health/status", headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            ms = int((time.time() - start) * 1000)
            data = json.loads(r.read())
        st = _status_from(data.get("status", "unknown"))
        results["imap_web_health"] = {"status": st, "label": f"{'Operational' if st == 'operational' else 'Degraded'} ({ms:,}ms)", "response_ms": ms}
    except Exception as e:
        _log_error("imap_web_health", e)
        results["imap_web_health"] = {"status": "outage", "label": "Major Outage", "response_ms": None}

    # API health + sub-components
    try:
        start = time.time()
        req = urllib.request.Request(f"{IMAP_BASE}/api/health/status", headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            ms = int((time.time() - start) * 1000)
            data = json.loads(r.read())
        st = _status_from(data.get("status", "unknown"))
        results["imap_api_health"] = {"status": st, "label": f"{'Operational' if st == 'operational' else 'Degraded'} ({ms:,}ms)", "response_ms": ms}

        comp_map = {
            "blob_storage": "imap_blob",
            "entra":        "imap_entra",
            "jira":         "imap_jira_conn",
            "snowflake":    "imap_snowflake",
        }
        checks = data.get("checks", {})
        for check_key, svc_key in comp_map.items():
            comp = checks.get(check_key, {})
            comp_st = _status_from(comp.get("status", "unknown"))
            comp_ms = comp.get("response_ms")
            ms_str = f" ({comp_ms:,}ms)" if comp_ms else ""
            label = ("Operational" if comp_st == "operational" else "Degraded" if comp_st == "degraded" else "Major Outage") + ms_str
            results[svc_key] = {"status": comp_st, "label": label, "response_ms": comp_ms}
    except Exception as e:
        _log_error("imap_api_health", e)
        for k in ["imap_api_health", "imap_blob", "imap_entra", "imap_jira_conn", "imap_snowflake"]:
            results[k] = {"status": "outage", "label": "Major Outage", "response_ms": None}

    return results


def fetch_slack_status():
    try:
        with urllib.request.urlopen(
            "https://slack-status.com/api/v2.0.0/current", timeout=5
        ) as r:
            data = json.loads(r.read())
        s = data.get("status", "unknown")
        if s == "ok":
            return {"status": "operational", "label": "Operational"}
        if s == "active":
            return {"status": "degraded", "label": "Degraded"}
        return {"status": "outage", "label": "Major Outage"}
    except Exception:
        return {"status": "unknown", "label": "Unknown"}



def fetch_jira_status():
    try:
        req = urllib.request.Request(
            "https://jira-service-management.status.atlassian.com/api/v2/status.json",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        indicator = data.get("status", {}).get("indicator", "unknown")
        if indicator == "none":
            return {"status": "operational", "label": "Operational"}
        if indicator == "minor":
            return {"status": "degraded", "label": "Degraded"}
        if indicator in ("major", "critical"):
            return {"status": "outage", "label": "Major Outage"}
        return {"status": "unknown", "label": "Unknown"}
    except Exception as e:
        _log_error("jira_status", e)
        return {"status": "unknown", "label": "Unknown"}


def fetch_greenhouse_status():
    """Fetches status for Greenhouse Recruiting, Harvest API, and Job Boards via component IDs."""
    COMPONENT_IDS = {
        "greenhouse_website":    "9g0xmmrmv083",
        "greenhouse_recruiting": "r54hn7khtvv1",
        "greenhouse_harvest":    "ghc9lfkqzz51",
        "greenhouse_jobboards":  "gsy7bj9ndl4d",
    }
    STATUS_MAP = {
        "operational":          {"status": "operational", "label": "Operational"},
        "degraded_performance": {"status": "degraded",    "label": "Degraded"},
        "partial_outage":       {"status": "partial",     "label": "Partial Outage"},
        "major_outage":         {"status": "outage",      "label": "Major Outage"},
    }
    try:
        req = urllib.request.Request(
            "https://status.greenhouse.io/api/v2/components.json",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        by_id = {c["id"]: c["status"] for c in data.get("components", [])}
        result = {}
        for key, cid in COMPONENT_IDS.items():
            raw = by_id.get(cid, "unknown")
            result[key] = STATUS_MAP.get(raw, {"status": "unknown", "label": "Unknown"})
        return result
    except Exception as e:
        _log_error("greenhouse_status", e)
        return {k: {"status": "unknown", "label": "Unknown"} for k in COMPONENT_IDS}


def fetch_leadsquared_status():
    try:
        url = "https://func-nat-gateway-app.azurewebsites.net/api/sync-status"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        status = data.get("status", "unknown")
        last_sync = data.get("lastSync", {})
        tables_total = last_sync.get("tablesTotal", 0)
        tables_failed = last_sync.get("tablesFailed", 0)
        tables_succeeded = last_sync.get("tablesSucceeded", 0)
        timestamp = last_sync.get("timestamp", "")
        errors = last_sync.get("errors", [])
        if status == "healthy" and tables_failed == 0:
            return {
                "status": "operational",
                "label": "Operational",
                "tables_total": tables_total,
                "tables_succeeded": tables_succeeded,
                "tables_failed": tables_failed,
                "last_sync": timestamp,
                "errors": errors,
            }
        elif tables_failed > 0:
            return {
                "status": "degraded",
                "label": "Degraded",
                "tables_total": tables_total,
                "tables_succeeded": tables_succeeded,
                "tables_failed": tables_failed,
                "last_sync": timestamp,
                "errors": errors,
            }
        else:
            return {
                "status": "unknown",
                "label": "Unknown",
                "tables_total": 0,
                "tables_succeeded": 0,
                "tables_failed": 0,
                "last_sync": None,
                "errors": [],
            }
    except Exception as e:
        _log_error("leadsquared_status", e)
        return {
            "status": "outage",
            "label": "Major Outage",
            "tables_total": 0,
            "tables_succeeded": 0,
            "tables_failed": 0,
            "last_sync": None,
            "errors": [],
        }


# --- Application Insights ---
_ai_token_cache: dict = {}

def _get_ai_token():
    """Get an Azure AD bearer token for Application Insights, cached until near expiry."""
    now = time.time()
    if _ai_token_cache.get("token") and now < _ai_token_cache.get("expires_at", 0) - 60:
        return _ai_token_cache["token"]
    payload = urllib.parse.urlencode({
        "grant_type":    "client_credentials",
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "scope":         "https://api.applicationinsights.io/.default",
    }).encode()
    req = urllib.request.Request(
        f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read())
    _ai_token_cache["token"] = data["access_token"]
    _ai_token_cache["expires_at"] = now + int(data.get("expires_in", 3600))
    return _ai_token_cache["token"]


def query_app_insights(kql):
    try:
        token = _get_ai_token()
        url = f"https://api.applicationinsights.io/v1/apps/{APPINSIGHTS_APP_ID}/query"
        params = urllib.parse.urlencode({"query": kql})
        req = urllib.request.Request(
            f"{url}?{params}", headers={"Authorization": f"Bearer {token}"}
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except Exception as e:
        _log_error("app_insights", e)
        return None


def get_optimus_daily_uptime():
    kql = """
    requests
    | where timestamp > ago(90d)
    | summarize total = count(), failed = countif(success == false) by bin(timestamp, 1d)
    | extend uptime_pct = round(100.0 * (total - failed) / total, 2)
    | order by timestamp asc
    """
    result = query_app_insights(kql)
    daily = {}
    try:
        for row in result["tables"][0]["rows"]:
            date = row[0][:10]
            daily[date] = row[3]
    except Exception as e:
        _log_error("daily_uptime", e)
    return daily


def get_db_daily_uptime(services):
    """Query PingResult grouped by calendar day for the last 90 days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=90)
    rows = PingResult.query.filter(
        PingResult.service.in_(services),
        PingResult.pinged_at >= cutoff,
    ).all()
    raw = {svc: {} for svc in services}
    for r in rows:
        date_key = r.pinged_at.strftime("%Y-%m-%d")
        bucket = raw[r.service].setdefault(date_key, {"total": 0, "success": 0})
        bucket["total"] += 1
        if r.success:
            bucket["success"] += 1
    return {
        svc: {d: round((c["success"] / c["total"]) * 100, 3) for d, c in days.items() if c["total"] > 0}
        for svc, days in raw.items()
    }


def get_optimus_uptime_from_insights():
    overall_kql = """
    requests
    | where timestamp > ago(90d)
    | summarize total = count(), failed = countif(success == false)
    | extend uptime_pct = round(100.0 * (total - failed) / total, 2)
    """
    detail_kql = """
    requests
    | where timestamp > ago(90d)
    | summarize total = count(), failed = countif(success == false) by name
    | extend uptime_pct = round(100.0 * (total - failed) / total, 2)
    """
    overall_result = query_app_insights(overall_kql)
    detail_result = query_app_insights(detail_kql)

    overall = None
    try:
        overall = overall_result["tables"][0]["rows"][0][2]
    except Exception:
        pass

    detail_uptimes = {}
    try:
        for row in detail_result["tables"][0]["rows"]:
            detail_uptimes[row[0]] = row[3]
    except Exception:
        pass

    return {
        "overall": overall,
    }


def get_optimus_24h_uptime_from_insights():
    """Returns {"overall": pct} for the last 24h."""
    overall_kql = """
    requests
    | where timestamp > ago(24h)
    | summarize total = count(), failed = countif(success == false)
    | extend uptime_pct = round(100.0 * (total - failed) / total, 2)
    """
    detail_kql = """
    requests
    | where timestamp > ago(24h)
    | summarize total = count(), failed = countif(success == false) by name
    | extend uptime_pct = round(100.0 * (total - failed) / total, 2)
    """
    overall_result = query_app_insights(overall_kql)
    detail_result  = query_app_insights(detail_kql)

    overall = None
    try:
        overall = overall_result["tables"][0]["rows"][0][2]
    except Exception:
        pass

    detail_uptimes = {}
    try:
        for row in detail_result["tables"][0]["rows"]:
            detail_uptimes[row[0]] = row[3]
    except Exception:
        pass

    return {
        "overall": overall,
    }


def get_optimus_hourly_uptime():
    """Returns per-endpoint hourly uptime for the last 24h.
    Keys: overall
    Each value is a dict of {hour_key: uptime_pct}.
    """
    kql = """
    requests
    | where timestamp > ago(24h)
    | summarize total = count(), failed = countif(success == false) by bin(timestamp, 1h)
    | extend uptime_pct = round(100.0 * (total - failed) / total, 2)
    | order by timestamp asc
    """
    result = query_app_insights(kql)

    buckets = {"overall": {}}
    overall_raw = {}  # hour -> [total, failed] for overall aggregation

    try:
        for row in result["tables"][0]["rows"]:
            hour   = row[0][:13]
            total  = row[1]
            failed = row[2]

            if hour not in overall_raw:
                overall_raw[hour] = [0, 0]
            overall_raw[hour][0] += total
            overall_raw[hour][1] += failed
    except Exception as e:
        _log_error("hourly_uptime", e)

    for hour, (total, failed) in overall_raw.items():
        buckets["overall"][hour] = round(100.0 * (total - failed) / total, 2) if total else 100.0

    return buckets


def get_hourly_uptime_data(service):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    results = PingResult.query.filter(
        PingResult.service == service,
        PingResult.pinged_at >= cutoff,
    ).all()
    hourly = {}
    for r in results:
        hour = r.pinged_at.strftime("%Y-%m-%dT%H")
        if hour not in hourly:
            hourly[hour] = {"total": 0, "success": 0}
        hourly[hour]["total"] += 1
        if r.success:
            hourly[hour]["success"] += 1
    return {
        hour: round((counts["success"] / counts["total"]) * 100, 2)
        for hour, counts in hourly.items()
    }


def _build_24h_slots():
    """Return a dict of {hour_key: 100} for the past 24 hours."""
    now = datetime.now(timezone.utc)
    base = now.replace(minute=0, second=0, microsecond=0)
    return {
        (base - timedelta(hours=23 - i)).strftime("%Y-%m-%dT%H"): 100
        for i in range(24)
    }


def get_slack_hourly_uptime():
    try:
        with urllib.request.urlopen(
            "https://slack-status.com/api/v2.0.0/history", timeout=10
        ) as r:
            incidents = json.loads(r.read())
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=24)
        hourly = _build_24h_slots()
        for incident in incidents:
            start_str = incident.get("date_created", "")
            end_str = incident.get("date_updated", start_str)
            try:
                start = datetime.fromisoformat(start_str).astimezone(timezone.utc)
                end = datetime.fromisoformat(end_str).astimezone(timezone.utc)
            except Exception:
                continue
            if end < cutoff or start > now:
                continue
            for hour_key, pct in hourly.items():
                hour_dt = datetime.strptime(hour_key, "%Y-%m-%dT%H").replace(tzinfo=timezone.utc)
                if start < hour_dt + timedelta(hours=1) and end > hour_dt:
                    hourly[hour_key] = min(pct, 0)
        return hourly
    except Exception as e:
        _log_error("slack_hourly", e)
        return {}


def get_statuspage_hourly_uptime(incidents_url):
    try:
        req = urllib.request.Request(incidents_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())
        incidents = data.get("incidents", [])
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=24)
        hourly = _build_24h_slots()
        for incident in incidents:
            start_str = incident.get("started_at") or incident.get("created_at", "")
            end_str = incident.get("resolved_at") or ""
            try:
                start = datetime.fromisoformat(start_str.replace("Z", "+00:00")).astimezone(timezone.utc)
                end = datetime.fromisoformat(end_str.replace("Z", "+00:00")).astimezone(timezone.utc) if end_str else now
            except Exception:
                continue
            if end < cutoff or start > now:
                continue
            impact = incident.get("impact", "minor")
            degraded_pct = 0 if impact in ("critical", "major") else 50
            for hour_key, pct in hourly.items():
                hour_dt = datetime.strptime(hour_key, "%Y-%m-%dT%H").replace(tzinfo=timezone.utc)
                if start < hour_dt + timedelta(hours=1) and end > hour_dt:
                    hourly[hour_key] = min(pct, degraded_pct)
        return hourly
    except Exception as e:
        _log_error("statuspage_hourly", f"{incidents_url}: {e}")
        return {}


# --- Alert notifications ---
def send_alert(service, uptime_pct, recovered=False):
    label = _MONITORED_LABELS.get(service, service.replace("_", " ").title())
    url   = _status_page_url()
    if recovered:
        text = (
            f"✅ *{label}* has recovered\n"
            f"_24h uptime is back at *{uptime_pct}%* — <{url}|view status page>_"
        )
    else:
        text = (
            f"🔴 *{label}* is degraded\n"
            f"_24h uptime dropped to *{uptime_pct}%* — <{url}|view status page>_"
        )
    _post_slack(text)
    print(f"Alert fired: {label} — {'recovered' if recovered else 'degraded'}")


def check_and_alert(service, uptime_pct):
    if uptime_pct is None:
        return
    state = AlertState.query.filter_by(service=service).first()
    if state is None:
        state = AlertState(service=service, in_alert=False)
        db.session.add(state)

    currently_degraded = uptime_pct < ALERT_THRESHOLD
    # Recovery uses recent ping status — fires as soon as service is back up,
    # not when the 24h rolling average climbs back above the threshold.
    currently_operational = get_latest_ping_status(service)["status"] == "operational"

    if currently_degraded and not state.in_alert:
        state.in_alert = True
        state.last_pct = uptime_pct
        state.alerted_at = datetime.now(timezone.utc)
        db.session.commit()
        send_alert(service, uptime_pct, recovered=False)
    elif currently_operational and state.in_alert:
        state.in_alert = False
        state.last_pct = uptime_pct
        db.session.commit()
        send_alert(service, uptime_pct, recovered=True)
    else:
        state.last_pct = uptime_pct
        db.session.commit()


# --- Background ping job ---
def run_pings():
    with app.app_context():
        # Pre-fetch iMAP components — single HTTP call for all 6 sub-services
        imap_comp = check_imap_components()
        for k, v in imap_comp.items():
            _latest_metrics[k] = {"response_ms": v.get("response_ms")}

        def _ping(service, url):
            if service == "optimus":
                return service, check_optimus_health()
            if service in IMAP_COMPONENT_KEYS:
                return service, imap_comp.get(service, {"status": "unknown", "label": "Unknown"})
            if service in CONTENT_PAGE_CHECKS:
                result = check_page_content(url, CONTENT_PAGE_CHECKS[service])
                _latest_metrics[service] = {"response_ms": result.get("response_ms")}
                return service, result
            return service, check_app_health(url)

        with ThreadPoolExecutor(max_workers=10) as ex:
            futures = {ex.submit(_ping, svc, url): svc for svc, url in MONITORED_ENDPOINTS.items()}
            for future in futures:
                service, result = future.result()
                db.session.add(PingResult(service=service, success=result["status"] == "operational"))

        # Career services sync check
        sync_result = check_optimus_sync()
        sync_ping = PingResult(service="optimus_sync", success=sync_result["status"] == "operational")
        db.session.add(sync_ping)
        db.session.commit()
        cutoff = datetime.now(timezone.utc) - timedelta(days=365)
        PingResult.query.filter(PingResult.pinged_at < cutoff).delete()
        ErrorLog.query.filter(ErrorLog.logged_at < datetime.now(timezone.utc) - timedelta(days=7)).delete()
        db.session.commit()

        # Check alerts for all monitored endpoints after every ping cycle
        for service in MONITORED_ENDPOINTS:
            check_and_alert(service, get_uptime(service, days=1))
        print(f"Pings recorded at {datetime.now(timezone.utc)}")


def warm_caches():
    """Pre-build status and hourly caches so API requests return instantly."""
    with app.app_context():
        try:
            data = _build_status()
            _cache["api_status"] = {"ts": time.time(), "data": data}
        except Exception as e:
            _log_error("cache_warm_status", e)
        try:
            data = _build_hourly()
            _cache["api_hourly"] = {"ts": time.time(), "data": data}
        except Exception as e:
            _log_error("cache_warm_hourly", e)


scheduler = BackgroundScheduler()
scheduler.add_job(run_pings,               "interval", minutes=5)
scheduler.add_job(run_third_party_alerts,  "interval", minutes=15)
scheduler.add_job(warm_caches,             "interval", seconds=25)
scheduler.start()


# --- Uptime from ping history ---
def uptime_pct_from_hourly(hourly):
    if not hourly:
        return None
    values = list(hourly.values())
    return round(sum(values) / len(values), 2)


def get_uptime(service, days=90):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    total = PingResult.query.filter(
        PingResult.service == service, PingResult.pinged_at >= cutoff
    ).count()
    if total == 0:
        return None
    success = PingResult.query.filter(
        PingResult.service == service,
        PingResult.pinged_at >= cutoff,
        PingResult.success == True,
    ).count()
    return round((success / total) * 100, 3)


def get_uptime_batch(services):
    """Single-pass DB query returning {service: {"uptime": pct, "uptime_24h": pct}}."""
    now = datetime.now(timezone.utc)
    result = {svc: {"uptime": None, "uptime_24h": None} for svc in services}
    for days, key in ((90, "uptime"), (1, "uptime_24h")):
        cutoff = now - timedelta(days=days)
        rows = (
            db.session.query(
                PingResult.service,
                db.func.count(PingResult.id).label("total"),
                db.func.sum(
                    db.case((PingResult.success == True, 1), else_=0)
                ).label("successes"),
            )
            .filter(PingResult.service.in_(services), PingResult.pinged_at >= cutoff)
            .group_by(PingResult.service)
            .all()
        )
        for row in rows:
            if row.total:
                result[row.service][key] = round((row.successes / row.total) * 100, 3)
    return result


def get_hourly_uptime_batch(services):
    """Single DB query returning {service: {hour_key: pct}} for the last 24h."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    rows = PingResult.query.filter(
        PingResult.service.in_(services),
        PingResult.pinged_at >= cutoff,
    ).all()
    raw = {svc: {} for svc in services}
    for r in rows:
        h = r.pinged_at.strftime("%Y-%m-%dT%H")
        bucket = raw[r.service].setdefault(h, {"total": 0, "success": 0})
        bucket["total"] += 1
        if r.success:
            bucket["success"] += 1
    return {
        svc: {h: round((c["success"] / c["total"]) * 100, 3) for h, c in hours.items()}
        for svc, hours in raw.items()
    }


# --- Auth helpers ---
def get_msal_app():
    return msal.ConfidentialClientApplication(
        CLIENT_ID, authority=AUTHORITY, client_credential=CLIENT_SECRET
    )


def is_logged_in():
    user = session.get("user", {})
    email = user.get("preferred_username", "").lower()
    return email in [a.lower() for a in AUTHORIZED_ADMINS]


# --- Routes ---
@app.route("/")
def index():
    return render_template("index.html")


def _build_status():
    """Core status computation — called by the API and the cache warmer."""
    with ThreadPoolExecutor(max_workers=8) as ex:
        f_slack   = ex.submit(lambda: _cached("slack_st",    120, fetch_slack_status))
        f_ls      = ex.submit(lambda: _cached("ls_st",       120, fetch_leadsquared_status))
        f_jira    = ex.submit(lambda: _cached("jira_st",     120, fetch_jira_status))
        f_gh      = ex.submit(lambda: _cached("gh_st",       120, fetch_greenhouse_status))
        f_slack_h = ex.submit(lambda: _cached("slack_h",     300, get_slack_hourly_uptime))
        f_ls_h    = ex.submit(lambda: _cached("ls_h",        300, lambda: get_statuspage_hourly_uptime("https://status.leadsquared.com/api/v2/incidents.json")))
        f_jira_h  = ex.submit(lambda: _cached("jira_h",      300, lambda: get_statuspage_hourly_uptime("https://jira-service-management.status.atlassian.com/api/v2/incidents.json")))
        f_gh_h    = ex.submit(lambda: _cached("gh_h",        300, lambda: get_statuspage_hourly_uptime("https://status.greenhouse.io/api/v2/incidents.json")))

    slack_st        = f_slack.result()
    leadsquared_st  = f_ls.result()
    jira_st         = f_jira.result()
    gh_st           = f_gh.result()

    gh_hourly = f_gh_h.result()
    slack_st["uptime_24h"]       = uptime_pct_from_hourly(f_slack_h.result())
    leadsquared_st["uptime_24h"] = uptime_pct_from_hourly(f_ls_h.result())
    jira_st["uptime_24h"]        = uptime_pct_from_hourly(f_jira_h.result())
    for key in gh_st:
        gh_st[key]["uptime_24h"] = uptime_pct_from_hourly(gh_hourly)

    status = {
        "slack":                 slack_st,
        "leadsquared":           leadsquared_st,
        "jira":                  jira_st,
        "greenhouse_website":    gh_st["greenhouse_website"],
        "greenhouse_recruiting": gh_st["greenhouse_recruiting"],
        "greenhouse_harvest":    gh_st["greenhouse_harvest"],
        "greenhouse_jobboards":  gh_st["greenhouse_jobboards"],
    }

    # Batch all uptime from DB (including Optimus — no longer using App Insights for %)
    batch_uptime = get_uptime_batch(list(MONITORED_ENDPOINTS.keys()))

    for service in MONITORED_ENDPOINTS:
        result = get_latest_ping_status(service)
        result["uptime"]     = batch_uptime[service]["uptime"]
        result["uptime_24h"] = batch_uptime[service]["uptime_24h"]
        if service in CONTENT_PAGE_CHECKS or service in IMAP_COMPONENT_KEYS:
            ms = (_latest_metrics.get(service) or {}).get("response_ms")
            if ms is not None:
                result["response_ms"] = ms
                if result["status"] == "operational":
                    result["label"] = f"Operational ({ms:,}ms)"
        status[service] = result

    return status


@app.route("/api/status")
@limiter.limit("60 per minute")
def api_status():
    cached = _cache.get("api_status")
    if cached and time.time() - cached["ts"] < 30:
        return jsonify(cached["data"])
    data = _build_status()
    _cache["api_status"] = {"ts": time.time(), "data": data}
    return jsonify(data)


@app.route("/api/daily-uptime")
@limiter.limit("10 per minute")
def api_daily_uptime():
    db_daily = get_db_daily_uptime([
        "optimus",
        "imap",
        "school_aviation", "school_centura", "school_tidewater",
        "aim_admissions", "aim_campuses", "aim_programs", "aim_resources", "aim_about",
    ])
    return jsonify(db_daily)


def _build_hourly():
    """Core hourly uptime computation — called by the API and the cache warmer."""
    # Single DB query for all ping-tracked services
    db_services = [
        "optimus",
        "imap", "imap_web_health", "imap_api_health",
        "imap_blob", "imap_entra", "imap_jira_conn", "imap_snowflake",
        "school_aviation", "school_centura", "school_tidewater",
        "aim_admissions", "aim_campuses", "aim_programs", "aim_resources", "aim_about",
    ]
    db_hourly = get_hourly_uptime_batch(db_services)

    with ThreadPoolExecutor(max_workers=5) as ex:
        f_slk  = ex.submit(lambda: _cached("slack_h",    300, get_slack_hourly_uptime))
        f_anth = ex.submit(lambda: _cached("anth_h",     300, lambda: get_statuspage_hourly_uptime("https://status.anthology.com/api/v2/incidents.json")))
        f_ls   = ex.submit(lambda: _cached("ls_h",       300, lambda: get_statuspage_hourly_uptime("https://status.leadsquared.com/api/v2/incidents.json")))
        f_jira = ex.submit(lambda: _cached("jira_h",     300, lambda: get_statuspage_hourly_uptime("https://jira-service-management.status.atlassian.com/api/v2/incidents.json")))
        f_gh   = ex.submit(lambda: _cached("gh_h",       300, lambda: get_statuspage_hourly_uptime("https://status.greenhouse.io/api/v2/incidents.json")))
    gh_hourly = f_gh.result()
    return {
        "optimus":               db_hourly["optimus"],
        "imap":                  db_hourly["imap"],
        "imap_web_health":       db_hourly["imap_web_health"],
        "imap_api_health":       db_hourly["imap_api_health"],
        "imap_blob":             db_hourly["imap_blob"],
        "imap_entra":            db_hourly["imap_entra"],
        "imap_jira_conn":        db_hourly["imap_jira_conn"],
        "imap_snowflake":        db_hourly["imap_snowflake"],
        "slack":                 f_slk.result(),
        "anthology":             f_anth.result(),
        "leadsquared":           f_ls.result(),
        "jira":                  f_jira.result(),
        "greenhouse_website":    gh_hourly,
        "greenhouse_recruiting": gh_hourly,
        "greenhouse_harvest":    gh_hourly,
        "greenhouse_jobboards":  gh_hourly,
        "school_aviation":       db_hourly["school_aviation"],
        "school_centura":        db_hourly["school_centura"],
        "school_tidewater":      db_hourly["school_tidewater"],
        "aim_admissions":        db_hourly["aim_admissions"],
        "aim_campuses":          db_hourly["aim_campuses"],
        "aim_programs":          db_hourly["aim_programs"],
        "aim_resources":         db_hourly["aim_resources"],
        "aim_about":             db_hourly["aim_about"],
    }


@app.route("/api/hourly-uptime")
@limiter.limit("30 per minute")
def api_hourly_uptime():
    cached = _cache.get("api_hourly")
    if cached and time.time() - cached["ts"] < 30:
        return jsonify(cached["data"])
    data = _build_hourly()
    _cache["api_hourly"] = {"ts": time.time(), "data": data}
    return jsonify(data)


def _fetch_slack_incidents(cutoff):
    items = []
    try:
        with urllib.request.urlopen(
            "https://slack-status.com/api/v2.0.0/history", timeout=10
        ) as r:
            data = json.loads(r.read())
        for inc in data:
            try:
                start = datetime.fromisoformat(inc["date_created"]).astimezone(timezone.utc)
                if start < cutoff:
                    continue
                end_str = inc.get("date_updated", "")
                end = datetime.fromisoformat(end_str).astimezone(timezone.utc) if end_str else None
                notes = inc.get("notes", [])
                last_body = notes[-1].get("body", "").lower() if notes else ""
                is_resolved = "resolved" in last_body or "all clear" in last_body
                items.append({
                    "service": "Slack",
                    "title": inc.get("title", "Incident"),
                    "status": "resolved" if is_resolved else "active",
                    "impact": "major",
                    "started_at": start.strftime("%b %d, %Y %-I:%M %p UTC"),
                    "resolved_at": end.strftime("%b %d, %Y %-I:%M %p UTC") if (end and is_resolved) else None,
                    "sort_key": start.isoformat(),
                })
            except Exception:
                continue
    except Exception as e:
        _log_error("slack_incidents", e)
    return items


def _fetch_statuspage_incidents(service_name, url, cutoff):
    items = []
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())
        for inc in data.get("incidents", []):
            try:
                start = datetime.fromisoformat(
                    (inc.get("started_at") or inc.get("created_at")).replace("Z", "+00:00")
                ).astimezone(timezone.utc)
                end_str = inc.get("resolved_at") or ""
                end = datetime.fromisoformat(end_str.replace("Z", "+00:00")).astimezone(timezone.utc) if end_str else None
                if end and end < cutoff:
                    continue
                if not end and start < cutoff:
                    continue
                items.append({
                    "service": service_name,
                    "title": inc.get("name", "Incident"),
                    "status": "resolved" if end else "active",
                    "impact": inc.get("impact", "minor"),
                    "started_at": start.strftime("%b %d, %Y %-I:%M %p UTC"),
                    "resolved_at": end.strftime("%b %d, %Y %-I:%M %p UTC") if end else None,
                    "sort_key": start.isoformat(),
                })
            except Exception:
                continue
    except Exception as e:
        _log_error(f"tp_incidents_{service_name.lower().replace(' ', '_')}", e)
    return items


@app.route("/api/third-party-incidents")
@limiter.limit("10 per minute")
def api_third_party_incidents():
    cached = _cache.get("third_party_incidents")
    if cached and time.time() - cached["ts"] < 300:
        return jsonify(cached["data"])

    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    statuspage_sources = [
        ("Anthology",              "https://status.anthology.com/api/v2/incidents.json"),
        ("LeadSquared",            "https://status.leadsquared.com/api/v2/incidents.json"),
        ("Jira Service Management","https://jira-service-management.status.atlassian.com/api/v2/incidents.json"),
        ("Greenhouse",             "https://status.greenhouse.io/api/v2/incidents.json"),
    ]

    with ThreadPoolExecutor(max_workers=5) as ex:
        f_slack = ex.submit(_fetch_slack_incidents, cutoff)
        sp_futures = [ex.submit(_fetch_statuspage_incidents, name, url, cutoff) for name, url in statuspage_sources]

    results = f_slack.result()
    for f in sp_futures:
        results.extend(f.result())

    results.sort(key=lambda x: x["sort_key"], reverse=True)
    for r in results:
        del r["sort_key"]

    _cache["third_party_incidents"] = {"ts": time.time(), "data": results}
    return jsonify(results)



@app.route("/history")
def history():
    return render_template("history.html")


@app.route("/api/incidents")
def api_incidents():
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    incidents = (
        Incident.query.filter(
            (Incident.status == "active")
            | ((Incident.status == "resolved") & (Incident.started_at >= cutoff))
        )
        .order_by(Incident.started_at.desc())
        .all()
    )
    return jsonify(
        [
            {
                "id": i.id,
                "service": i.service,
                "severity": i.severity,
                "description": i.description,
                "status": i.status,
                "started_at": (
                    i.started_at.strftime("%b %d, %Y") if i.started_at else None
                ),
                "resolved_at": (
                    i.resolved_at.strftime("%b %d, %Y") if i.resolved_at else None
                ),
            }
            for i in incidents
        ]
    )


# --- Admin routes ---
@app.route("/admin/login")
def admin_login():
    flow = get_msal_app().initiate_auth_code_flow(SCOPE, redirect_uri=REDIRECT_URI)
    session["flow"] = flow
    return redirect(flow["auth_uri"])


@app.route("/auth/callback")
def auth_callback():
    try:
        result = get_msal_app().acquire_token_by_auth_code_flow(
            session.get("flow", {}), request.args
        )
        if "access_token" in result:
            claims = result.get("id_token_claims", {})
            email = claims.get("preferred_username", "").lower()
            if email not in [a.lower() for a in AUTHORIZED_ADMINS]:
                session.clear()
                return "Access denied — you are not an authorized admin.", 403
            session["user"] = claims
            return redirect(url_for("index"))
        return f"Login failed: {result.get('error_description')}", 401
    except Exception as e:
        return f"Auth error: {e}", 500


@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/admin/incidents", methods=["POST"])
def create_incident():
    if not is_logged_in():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json()
    incident = Incident(
        service=data["service"],
        severity=data["severity"],
        description=data["description"],
        status="active",
    )
    db.session.add(incident)
    db.session.commit()
    return jsonify({"success": True, "id": incident.id})


@app.route("/admin/incidents/<int:incident_id>/resolve", methods=["POST"])
def resolve_incident(incident_id):
    if not is_logged_in():
        return jsonify({"error": "Unauthorized"}), 401
    incident = Incident.query.get_or_404(incident_id)
    incident.status = "resolved"
    incident.resolved_at = datetime.now(timezone.utc)
    db.session.commit()
    return jsonify({"success": True})


@app.route("/admin/incidents/<int:incident_id>", methods=["DELETE"])
def delete_incident(incident_id):
    if not is_logged_in():
        return jsonify({"error": "Unauthorized"}), 401
    incident = Incident.query.get_or_404(incident_id)
    db.session.delete(incident)
    db.session.commit()
    return jsonify({"success": True})


@app.route("/admin/error-logs")
def get_error_logs():
    if not is_logged_in():
        return jsonify({"error": "Unauthorized"}), 401
    logs = (
        ErrorLog.query
        .order_by(ErrorLog.logged_at.desc())
        .limit(100)
        .all()
    )
    return jsonify([{
        "id":        l.id,
        "logged_at": l.logged_at.strftime("%b %d, %Y %-I:%M:%S %p UTC"),
        "source":    l.source,
        "message":   l.message,
    } for l in logs])


@app.route("/admin/error-logs", methods=["DELETE"])
def clear_error_logs():
    if not is_logged_in():
        return jsonify({"error": "Unauthorized"}), 401
    ErrorLog.query.delete()
    db.session.commit()
    return jsonify({"success": True})


if __name__ == "__main__":
    app.run(debug=False)
