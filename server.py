"""Cashew — the little SaaS backend.

Serves the landing page and captures waitlist emails into a tiny SQLite file.
No tracking, no third parties — just a squirrel keeping a list.

Run it:
    pip install -r requirements.txt
    python server.py
    # then open http://127.0.0.1:8000
"""
import base64
import os
import re
import secrets
import threading
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

import store
from giftfinder.detect import detect
from giftfinder.report import dedupe
from giftfinder.sources import email_from_bytes, iter_mbox

os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

BASE = Path(__file__).parent
WEB = BASE / "web"
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
# Deliberately wide net — recall over precision. Gmail searches the full text
# of every email server-side; the detector filters false positives afterwards.
GMAIL_QUERY = " OR ".join(
    f'"{t}"' if " " in t else t
    for t in [
        "gift card", "e-gift", "egift", "egift card", "gift certificate",
        "gift voucher", "voucher", "e-voucher", "digital gift", "evoucher",
        "store credit", "account credit", "merchandise credit", "travel credit",
        "wallet credit", "in-store credit", "credit balance", "your balance",
        "available balance", "remaining balance", "you earned", "you've earned",
        "reward credit", "rewards balance", "bonus credit", "in points",
        "points balance", "cashback", "cash back", "loyalty points",
        "added to your account", "added to your wallet", "credited to your",
        "you've got", "redeem", "refund", "credited",
    ]
)
# Launch-day social-proof seed for the "early diggers" counter.
# Set to 0 to show the true signup count only.
SEED_DIGGERS = 1283
# What the average inbox is estimated to be hoarding (display stat).
AVG_STASH = 175

# Unlock tiers (the upsell). Prices are display-only here; wire to Stripe to
# charge for real (see _unlock below).
TIERS = {
    "once": {"label": "Unlock this dig", "price": "$4", "blurb": "See every brand + exactly how to claim each one."},
    "pro": {"label": "Cashew Pro", "price": "$9/mo", "blurb": "Unlock everything + Cashew watches your inbox 24/7, squeaks before anything expires, and keeps a tidy redeem checklist.", "recommended": True},
    "forever": {"label": "Forever burrow", "price": "$49", "blurb": "Unlock + lifetime monitoring. One acorn, forever."},
}

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

app = FastAPI(title="Cashew", docs_url=None, redoc_url=None)
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


GMAIL_SCAN_LIMIT = int(os.environ.get("GMAIL_SCAN_LIMIT", "25"))


def _run_gmail_scan(creds_dict: dict, limit: int = GMAIL_SCAN_LIMIT) -> list:
    """Fetch money-bearing mail read-only via the Gmail API and run detection.

    Emails are parsed and scored in memory and discarded — nothing is persisted.
    Limit kept modest so a scan finishes within serverless function timeouts.
    """
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials(**creds_dict)
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    findings = []
    seen = 0
    page_token = None
    # Page through every matching message. limit=0 means "no cap — scan it all".
    while True:
        listing = (
            service.users()
            .messages()
            .list(userId="me", q=GMAIL_QUERY, maxResults=100, pageToken=page_token)
            .execute()
        )
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
            seen += 1
            if limit and seen >= limit:
                break
        page_token = listing.get("nextPageToken")
        if not page_token or (limit and seen >= limit):
            break
    return [_to_dict(f) for f in dedupe(findings)]


# --- Deep scan (every email) with live progress ---------------------------
# job_id -> {scanned, total, found, done, error, scan_id, email}
_SCAN_JOBS: dict = {}
# GMAIL_DEEP=1 reads EVERY message (slow on big inboxes). =0 uses Gmail search
# to grab only money-bearing mail (fast, finds the same stashes).
GMAIL_DEEP = os.environ.get("GMAIL_DEEP", "0") == "1"


def _deep_scan_worker(job_id: str, creds_dict: dict, email: str) -> None:
    job = _SCAN_JOBS[job_id]
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds = Credentials(**creds_dict)
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        query = None if GMAIL_DEEP else GMAIL_QUERY

        # 1) Collect message ids (fast — ids only, no bodies).
        ids = []
        page_token = None
        while True:
            listing = service.users().messages().list(
                userId="me", q=query, maxResults=500, pageToken=page_token
            ).execute()
            ids += [m["id"] for m in listing.get("messages", [])]
            page_token = listing.get("nextPageToken")
            if not page_token:
                break
        job["total"] = len(ids)
        print(f"[pip] deep={GMAIL_DEEP} candidates={len(ids)}", flush=True)

        # 2) Batch-download 100 at a time (≈100× fewer round-trips) + detect.
        findings = []

        def _cb(request_id, response, exception):
            job["scanned"] += 1
            if exception is None and response and "raw" in response:
                try:
                    raw = base64.urlsafe_b64decode(response["raw"].encode("utf-8"))
                    f = detect(email_from_bytes(raw), min_confidence=0.45)
                    if f:
                        findings.append(f)
                        job["found"] = len(findings)
                except Exception:
                    pass

        for i in range(0, len(ids), 100):
            batch = service.new_batch_http_request(callback=_cb)
            for mid in ids[i:i + 100]:
                batch.add(
                    service.users().messages().get(userId="me", id=mid, format="raw")
                )
            batch.execute()

        items = [_to_dict(f) for f in dedupe(findings)]
        scan_id = secrets.token_urlsafe(9)
        store.save_scan(scan_id, email, items)
        job["scan_id"] = scan_id
        job["found"] = len(items)
        job["done"] = True
    except Exception as e:  # noqa: BLE001
        job["error"] = str(e)
        job["done"] = True


try:
    store.init()
except Exception as e:  # noqa: BLE001 — don't crash boot if the DB is briefly unavailable
    print(f"[pip] store.init failed ({store.backend()}): {e}")


def _redeem_hint(brand: str, kind: str) -> str:
    if kind == "gift_card":
        return f"Sign in to {brand} → Account → Gift cards, and redeem the code sitting in this email."
    if kind == "store_credit":
        return f"Your {brand} credit applies automatically at checkout — just shop."
    if kind == "referral_reward":
        return f"Open your {brand} app → Rewards to spend it before it lapses."
    return f"Open your {brand} account to claim it."


def _gmail_link(message_id: str, subject: str) -> str:
    """A link that opens the exact source email in Gmail."""
    import urllib.parse

    mid = (message_id or "").strip().strip("<>")
    if mid:
        return (
            "https://mail.google.com/mail/u/0/#search/"
            + urllib.parse.quote("rfc822msgid:" + mid)
        )
    return "https://mail.google.com/mail/u/0/#search/" + urllib.parse.quote(
        subject or "gift card"
    )


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
        "link": _gmail_link(getattr(f, "message_id", ""), f.subject),
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
    return {"diggers": SEED_DIGGERS + store.waitlist_count(), "avg_stash": AVG_STASH}


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
    n = store.add_waitlist(email)
    return {"ok": True, "diggers": SEED_DIGGERS + n}


@app.post("/api/scan/start")
async def scan_start(request: Request):
    """Kick off a background deep scan of the whole inbox; returns a job id."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    email = (body.get("email") or "").strip().lower()
    if email and EMAIL_RE.match(email):
        store.add_waitlist(email)
    creds = request.session.get("gmail_creds")
    if not creds:
        return JSONResponse(
            {"error": "Gmail isn't connected — tap “Dig up my money” to connect 🐿️"},
            status_code=400,
        )
    job_id = secrets.token_urlsafe(9)
    _SCAN_JOBS[job_id] = {
        "scanned": 0, "total": 0, "found": 0, "done": False,
        "error": None, "scan_id": None, "started": time.time(),
    }
    threading.Thread(
        target=_deep_scan_worker, args=(job_id, creds, email), daemon=True
    ).start()
    return {"job_id": job_id}


@app.get("/api/scan/progress/{job_id}")
def scan_progress(job_id: str):
    job = _SCAN_JOBS.get(job_id)
    if not job:
        return JSONResponse({"error": "unknown job"}, status_code=404)
    return {
        "scanned": job["scanned"],
        "total": job["total"],
        "found": job["found"],
        "done": job["done"],
        "error": job["error"],
        "scan_id": job["scan_id"],
    }


@app.get("/api/scan/result/{scan_id}")
def scan_result(scan_id: str):
    rec = store.get_scan(scan_id)
    if rec is None:
        return JSONResponse({"error": "that dig has wandered off 🐿️"}, status_code=404)
    items = rec["items"]
    if not items:
        return {
            "scan_id": scan_id, "count": 0, "total": 0, "currency": "USD",
            "free": None, "locked": [], "tiers": TIERS, "paid": False,
            "source": "gmail", "empty": True,
        }
    teaser = _teaser(scan_id, items)
    teaser["source"] = "gmail"
    return teaser


@app.post("/api/scan")
async def scan(request: Request):
    """One-shot scan (sample inbox / capped). The main UI uses /api/scan/start.

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
        store.add_waitlist(email)

    if source == "gmail":
        creds = request.session.get("gmail_creds")
        if not creds:
            return JSONResponse(
                {"error": "Gmail isn't connected — tap “Dig up my money” to connect 🐿️"},
                status_code=400,
            )
        try:
            items = _run_gmail_scan(creds)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": f"Cashew couldn't read your inbox: {e}"}, status_code=502
            )
    else:
        items = _run_sample_scan()

    scan_id = secrets.token_urlsafe(9)
    store.save_scan(scan_id, email, items)

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
    return {
        "connected": bool(request.session.get("gmail_creds")),
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
    # PKCE: the library put a code_challenge in the URL, so we must send the
    # matching verifier back at token-exchange time. Stash it in the session.
    request.session["code_verifier"] = flow.code_verifier
    return RedirectResponse(auth_url)


@app.get("/auth/google/callback")
def google_callback(request: Request):
    from google_auth_oauthlib.flow import Flow

    state = request.session.get("oauth_state")
    flow = Flow.from_client_config(
        _client_config(), scopes=GMAIL_SCOPES, redirect_uri=OAUTH_REDIRECT, state=state
    )
    flow.code_verifier = request.session.get("code_verifier")
    try:
        flow.fetch_token(authorization_response=str(request.url))
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"[pip] oauth callback failed: {e!r}", flush=True)
        return RedirectResponse("/?gmail=error")
    creds = flow.credentials
    # Stored in the signed session cookie (not server memory) so it survives
    # across serverless instances. It's the user's own token in their own cookie.
    request.session["gmail_creds"] = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": creds.scopes,
    }
    return RedirectResponse("/?connected=1")


@app.post("/auth/disconnect")
def disconnect(request: Request):
    request.session.pop("gmail_creds", None)
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

    rec = store.get_scan(scan_id)
    if not rec:
        return JSONResponse(
            {"ok": False, "error": "that dig has wandered off 🐿️"},
            status_code=404,
        )
    store.mark_paid(scan_id, tier)

    items = rec["items"]
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
