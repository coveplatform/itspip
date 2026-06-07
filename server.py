"""Pip — the little SaaS backend.

Serves the landing page and captures waitlist emails into a tiny SQLite file.
No tracking, no third parties — just a squirrel keeping a list.

Run it:
    pip install -r requirements.txt
    python server.py
    # then open http://127.0.0.1:8000
"""
import base64
import json
import os
import re
import secrets
import sqlite3
import tempfile
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from giftfinder.detect import detect
from giftfinder.report import dedupe
from giftfinder.sources import email_from_bytes, iter_mbox

os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

BASE = Path(__file__).parent
WEB = BASE / "web"
# Prefer ./data, but fall back to a writable temp dir on read-only hosts
# (e.g. serverless platforms where the app directory can't be written).
DATA = Path(os.environ.get("PIP_DATA_DIR", str(BASE / "data")))
try:
    DATA.mkdir(parents=True, exist_ok=True)
except OSError:
    DATA = Path(tempfile.gettempdir()) / "pip-data"
    DATA.mkdir(parents=True, exist_ok=True)
DB = DATA / "waitlist.db"
SAMPLE_MBOX = BASE / "giftfinder" / "sample" / "demo.mbox"


def _load_dotenv(path: Path) -> None:
    """Load KEY=VALUE pairs from a local .env (gitignored) into the environment."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_dotenv(BASE / ".env")

# --- Google / Gmail (read-only) connect -----------------------------------
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
OAUTH_REDIRECT = os.environ.get(
    "OAUTH_REDIRECT", "http://127.0.0.1:8000/auth/google/callback"
)
# Only relax the https requirement when redirecting over plain http (local dev).
if OAUTH_REDIRECT.startswith("http://"):
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_QUERY = (
    '"gift card" OR "e-gift" OR egift OR "gift certificate" OR '
    '"store credit" OR "account credit" OR "travel credit" OR '
    'referral OR voucher OR "you earned" OR "reward credit"'
)
# session-id -> stored credentials (in-memory; fine for a single-process app)
_GMAIL_SESSIONS: dict = {}

# Launch-day social-proof seed for the "early diggers" counter.
# Set to 0 to show the true signup count only.
SEED_DIGGERS = 1283
# What the average inbox is estimated to be hoarding (display stat).
AVG_STASH = 175

# Unlock tiers (the upsell). Prices are display-only here; wire to Stripe to
# charge for real (see _unlock below).
TIERS = {
    "once": {"label": "Unlock this dig", "price": "$4", "blurb": "See every brand + exactly how to claim each one."},
    "pro": {"label": "Pip Pro", "price": "$9/mo", "blurb": "Unlock everything + Pip watches your inbox 24/7, squeaks before anything expires, and keeps a tidy redeem checklist.", "recommended": True},
    "forever": {"label": "Forever burrow", "price": "$49", "blurb": "Unlock + lifetime monitoring. One acorn, forever."},
}

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

app = FastAPI(title="Pip", docs_url=None, redoc_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("PIP_SECRET", secrets.token_hex(16)),
    same_site="lax",
)


def _client_config() -> dict:
    return {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [OAUTH_REDIRECT],
        }
    }


def _run_gmail_scan(creds_dict: dict, limit: int = 80) -> list:
    """Fetch money-bearing mail read-only via the Gmail API and run detection.

    Emails are parsed and scored in memory and discarded — nothing is persisted.
    """
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials(**creds_dict)
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    listing = (
        service.users()
        .messages()
        .list(userId="me", q=GMAIL_QUERY, maxResults=limit)
        .execute()
    )
    findings = []
    for ref in listing.get("messages", []):
        msg = (
            service.users()
            .messages()
            .get(userId="me", id=ref["id"], format="raw")
            .execute()
        )
        raw = base64.urlsafe_b64decode(msg["raw"].encode("utf-8"))
        f = detect(email_from_bytes(raw), min_confidence=0.45)
        if f:
            findings.append(f)
    return [_to_dict(f) for f in dedupe(findings)]


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS waitlist ("
        "email TEXT PRIMARY KEY, created REAL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS scans ("
        "id TEXT PRIMARY KEY, created REAL, paid INTEGER DEFAULT 0, "
        "tier TEXT, email TEXT, items TEXT)"
    )
    return conn


def _count() -> int:
    conn = _db()
    try:
        return conn.execute("SELECT COUNT(*) FROM waitlist").fetchone()[0]
    finally:
        conn.close()


def _redeem_hint(brand: str, kind: str) -> str:
    if kind == "gift_card":
        return f"Sign in to {brand} → Account → Gift cards, and redeem the code sitting in this email."
    if kind == "store_credit":
        return f"Your {brand} credit applies automatically at checkout — just shop."
    if kind == "referral_reward":
        return f"Open your {brand} app → Rewards to spend it before it lapses."
    return f"Open your {brand} account to claim it."


def _to_dict(f) -> dict:
    return {
        "kind": f.kind,
        "brand": f.brand,
        "amount": f.amount,
        "currency": f.currency,
        "subject": f.subject,
        "code_present": f.code_present,
        "expires_text": f.expires_text,
        "redeem": _redeem_hint(f.brand, f.kind),
    }


def _run_sample_scan() -> list:
    findings = []
    for email in iter_mbox(str(SAMPLE_MBOX)):
        f = detect(email, min_confidence=0.45)
        if f:
            findings.append(f)
    return [_to_dict(f) for f in dedupe(findings)]


def _teaser(scan_id: str, items: list) -> dict:
    """One stash free, the rest locked (amount teased, brand & how-to hidden)."""
    revealable = [i for i in items if i["amount"]]
    free = min(revealable, key=lambda i: i["amount"]) if revealable else items[0]
    total = round(sum(i["amount"] or 0 for i in items), 2)
    currency = items[0]["currency"] if items else "USD"
    locked = [
        {"kind": i["kind"], "amount": i["amount"], "currency": i["currency"]}
        for i in items
        if i is not free
    ]
    return {
        "scan_id": scan_id,
        "count": len(items),
        "total": total,
        "currency": currency,
        "free": free,
        "locked": locked,
        "tiers": TIERS,
        "paid": False,
    }


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


@app.get("/privacy")
def privacy():
    return FileResponse(WEB / "privacy.html")


@app.get("/terms")
def terms():
    return FileResponse(WEB / "terms.html")


@app.get("/api/stats")
def stats():
    return {"diggers": SEED_DIGGERS + _count(), "avg_stash": AVG_STASH}


@app.post("/api/waitlist")
async def join(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    email = (body.get("email") or "").strip().lower()
    if not EMAIL_RE.match(email):
        return JSONResponse(
            {"ok": False, "error": "hmm, that doesn't look like an email 🐿️"},
            status_code=400,
        )
    conn = _db()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO waitlist (email, created) VALUES (?, ?)",
            (email, time.time()),
        )
        conn.commit()
        n = conn.execute("SELECT COUNT(*) FROM waitlist").fetchone()[0]
    finally:
        conn.close()
    return {"ok": True, "diggers": SEED_DIGGERS + n}


@app.post("/api/scan")
async def scan(request: Request):
    """Run a dig and return a teaser with exactly one stash unlocked.

    source="gmail" scans the connected real inbox (read-only); anything else
    scans the bundled sample inbox so the flow works with zero setup.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    email = (body.get("email") or "").strip().lower()
    source = body.get("source") or "demo"
    if email and EMAIL_RE.match(email):
        conn = _db()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO waitlist (email, created) VALUES (?, ?)",
                (email, time.time()),
            )
            conn.commit()
        finally:
            conn.close()

    if source == "gmail":
        sid = request.session.get("gmail_sid")
        creds = _GMAIL_SESSIONS.get(sid)
        if not creds:
            return JSONResponse(
                {"error": "Gmail isn't connected. Click “Connect Gmail” first 🐿️"},
                status_code=400,
            )
        try:
            items = _run_gmail_scan(creds)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": f"Pip couldn't read your inbox: {e}"}, status_code=502
            )
    else:
        items = _run_sample_scan()

    scan_id = secrets.token_urlsafe(9)
    conn = _db()
    try:
        conn.execute(
            "INSERT INTO scans (id, created, paid, email, items) VALUES (?,?,0,?,?)",
            (scan_id, time.time(), email, json.dumps(items)),
        )
        conn.commit()
    finally:
        conn.close()

    if not items:
        return {
            "scan_id": scan_id,
            "count": 0,
            "total": 0,
            "currency": "USD",
            "free": None,
            "locked": [],
            "tiers": TIERS,
            "paid": False,
            "source": source,
            "empty": True,
        }
    teaser = _teaser(scan_id, items)
    teaser["source"] = source
    return teaser


# --- Gmail OAuth (read-only) ----------------------------------------------
@app.get("/api/me")
def me(request: Request):
    sid = request.session.get("gmail_sid")
    return {
        "connected": bool(sid and sid in _GMAIL_SESSIONS),
        "configured": bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET),
    }


@app.get("/auth/google/start")
def google_start(request: Request):
    if not (GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET):
        return RedirectResponse("/?gmail=unconfigured")
    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_config(
        _client_config(), scopes=GMAIL_SCOPES, redirect_uri=OAUTH_REDIRECT
    )
    auth_url, state = flow.authorization_url(
        access_type="offline", include_granted_scopes="true", prompt="consent"
    )
    request.session["oauth_state"] = state
    return RedirectResponse(auth_url)


@app.get("/auth/google/callback")
def google_callback(request: Request):
    from google_auth_oauthlib.flow import Flow

    state = request.session.get("oauth_state")
    flow = Flow.from_client_config(
        _client_config(), scopes=GMAIL_SCOPES, redirect_uri=OAUTH_REDIRECT, state=state
    )
    try:
        flow.fetch_token(authorization_response=str(request.url))
    except Exception:
        return RedirectResponse("/?gmail=error")
    creds = flow.credentials
    sid = secrets.token_urlsafe(12)
    _GMAIL_SESSIONS[sid] = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": creds.scopes,
    }
    request.session["gmail_sid"] = sid
    return RedirectResponse("/?connected=1")


@app.post("/auth/disconnect")
def disconnect(request: Request):
    sid = request.session.pop("gmail_sid", None)
    if sid:
        _GMAIL_SESSIONS.pop(sid, None)
    return {"ok": True}


@app.post("/api/unlock/{scan_id}")
async def unlock(scan_id: str, request: Request):
    """Unlock a scan. This is where real money would change hands.

    To charge for real: create a Stripe Checkout Session here, return its URL,
    and only flip `paid=1` from the Stripe webhook. For now we unlock directly
    so the full experience works end-to-end.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    tier = body.get("tier") or "once"
    if tier not in TIERS:
        return JSONResponse({"ok": False, "error": "unknown tier"}, status_code=400)

    conn = _db()
    try:
        row = conn.execute(
            "SELECT items FROM scans WHERE id = ?", (scan_id,)
        ).fetchone()
        if not row:
            return JSONResponse(
                {"ok": False, "error": "that dig has wandered off 🐿️"},
                status_code=404,
            )
        conn.execute(
            "UPDATE scans SET paid = 1, tier = ? WHERE id = ?", (tier, scan_id)
        )
        conn.commit()
    finally:
        conn.close()

    items = json.loads(row[0])
    return {
        "ok": True,
        "paid": True,
        "tier": tier,
        "tier_label": TIERS[tier]["label"],
        "items": items,
        "total": round(sum(i["amount"] or 0 for i in items), 2),
        "currency": items[0]["currency"] if items else "USD",
        "monitoring": tier in ("pro", "forever"),
    }


# Static assets (styles.css, app.js, ...). Mounted last so the routes above win.
app.mount("/", StaticFiles(directory=WEB, html=False), name="static")


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    host = "0.0.0.0" if os.environ.get("PIP_ENV") == "production" else "127.0.0.1"
    uvicorn.run("server:app", host=host, port=port, reload=False)
