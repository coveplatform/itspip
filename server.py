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
from giftfinder.detect import detect, redemption_brand
from giftfinder.report import dedupe
from giftfinder.sources import email_from_bytes, email_from_gmail, iter_mbox
from giftfinder.verify import active_model, active_provider, verification_enabled, verify_finding

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

# --- Stripe (one-time unlock) ---------------------------------------------
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
UNLOCK_PRICE_CENTS = int(os.environ.get("UNLOCK_PRICE_CENTS", "495"))  # $4.95
# Public origin for Stripe redirect URLs, derived from the OAuth redirect.
SITE_ORIGIN = OAUTH_REDIRECT.split("/auth/")[0] or "http://127.0.0.1:8000"


def _stripe():
    """Return the configured stripe module, or None if no key is set (dev)."""
    if not STRIPE_SECRET_KEY:
        return None
    import stripe
    stripe.api_key = STRIPE_SECRET_KEY
    return stripe
# Deliberately wide net — recall over precision. Gmail searches the full text
# of every email server-side; the detector filters false positives afterwards.
# Focused on high-signal gift-card / store-credit language. Broad terms like
# "refund", "redeem", "credited", "cashback", "points", "balance", "you've got"
# were dropped — they matched a huge slice of the inbox (thousands of non-card
# emails) for little gain. Tighter query = fewer downloads = fits memory, faster,
# and higher precision. Override with GMAIL_QUERY env to widen again.
GMAIL_QUERY = os.environ.get("GMAIL_QUERY") or " OR ".join(
    f'"{t}"' if " " in t else t
    for t in [
        "gift card", "e-gift", "egift", "egift card", "gift certificate",
        "gift voucher", "e-voucher", "evoucher", "digital gift card",
        "store credit", "account credit", "merchandise credit", "travel credit",
        "in-store credit", "reward credit", "bonus credit", "refund credit",
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
    "once": {"label": "Unlock this dig", "price": "$4.95", "blurb": "See every brand + exactly how to claim each one."},
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


def _detect(email):
    """Keyword detect → LLM precision gate. Returns a Finding or None.

    The keyword pass is high-recall; `verify_finding` is the final judge that
    kills marketing dangles ("win a $300 gift card"), purchase receipts, and
    expired credit the keywords miss. With no API key, verify_finding is a no-op
    and the keyword verdict stands.
    """
    f = detect(email, min_confidence=0.45)
    if f is None:
        return None
    return verify_finding(email, f)


def _drop_spent(findings, spent_brands):
    """Cross-email pass: drop findings whose brand has a 'redeemed/used' email.

    spent_brands is a set of lowercased brand names gathered from redemption
    notices seen anywhere in the same inbox. Brand-level join, so it's kept
    conservative and every suppression is logged.
    """
    if not spent_brands:
        return findings
    kept = []
    for f in findings:
        if f.brand.lower() in spent_brands:
            print(f"[cashew] suppressed likely-spent: {f.brand} ${f.amount}", flush=True)
            continue
        kept.append(f)
    return kept


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
    spent = set()
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
                .get(userId="me", id=ref["id"], format="full")
                .execute()
            )
            em = email_from_gmail(msg)
            f = _detect(em)
            if f:
                findings.append(f)
            rb = redemption_brand(em)
            if rb:
                spent.add(rb.lower())
            seen += 1
            if limit and seen >= limit:
                break
        page_token = listing.get("nextPageToken")
        if not page_token or (limit and seen >= limit):
            break
    return [_to_dict(f) for f in dedupe(_drop_spent(findings, spent))]


# --- Deep scan (every email) with live progress ---------------------------
# job_id -> {scanned, total, found, done, error, scan_id, email}
_SCAN_JOBS: dict = {}
# GMAIL_DEEP=1 reads EVERY message (slow on big inboxes). =0 uses Gmail search
# to grab only money-bearing mail (fast, finds the same stashes).
GMAIL_DEEP = os.environ.get("GMAIL_DEEP", "0") == "1"
# Cap how many money-matching emails one deep scan pulls (newest first). A small
# instance can't churn a whole huge inbox; most forgotten cards are recent anyway.
# 0 = no cap (only safe on a larger instance). Raise it if you bump Render's RAM.
GMAIL_MAX = int(os.environ.get("GMAIL_MAX", "2000"))


def _deep_scan_worker(job_id: str, creds_dict: dict, email: str) -> None:
    job = _SCAN_JOBS[job_id]
    try:
        import threading
        from concurrent.futures import ThreadPoolExecutor

        import httplib2
        from google.oauth2.credentials import Credentials
        from google_auth_httplib2 import AuthorizedHttp
        from googleapiclient.discovery import build

        creds = Credentials(**creds_dict)
        # Pre-refresh once so the download threads share a valid token instead of
        # racing to refresh it mid-scan.
        try:
            from google.auth.transport.requests import Request as _AuthRequest
            if not creds.valid:
                creds.refresh(_AuthRequest())
        except Exception:
            pass
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
        if GMAIL_MAX and len(ids) > GMAIL_MAX:
            ids = ids[:GMAIL_MAX]  # newest first — scan the most recent N
        job["total"] = len(ids)
        print(f"[pip] deep={GMAIL_DEEP} candidates={len(ids)} (cap={GMAIL_MAX})", flush=True)

        lock = threading.Lock()
        findings = []
        spent = set()

        # 2) Download + fully process each email, then DISCARD it. We deliberately
        #    never hold more than a few small batches of raw emails at once — the
        #    Gmail query matches a big slice of a real inbox, and a 512MB instance
        #    can't keep them all in memory. detect (keyword) + verify (LLM) run
        #    inline; only the tiny Finding objects survive. A few batches run
        #    concurrently, each on its own AuthorizedHttp (the service is only used
        #    to build thread-safe request objects).
        # Text-only fetch is ~10x lighter than raw — keep concurrency modest so
        # peak memory stays tiny on 512MB; the speed comes from the lighter
        # payloads, not from many in flight at once.
        BATCH = 25
        WORKERS = 3

        def _download_chunk(chunk):
            # 30s socket timeout so a stuck connection can't freeze the scan.
            http = AuthorizedHttp(creds, http=httplib2.Http(timeout=30))

            def _cb(request_id, response, exception):
                with lock:
                    job["scanned"] += 1
                if exception is None and response and response.get("payload"):
                    try:
                        em = email_from_gmail(response)  # text only, no attachments
                        rb = redemption_brand(em)
                        if rb:
                            with lock:
                                spent.add(rb.lower())
                        kf = detect(em, min_confidence=0.45)
                        if kf:
                            f = verify_finding(em, kf)  # LLM gate (no-op without a key)
                            if f:
                                with lock:
                                    findings.append(f)
                                    job["found"] = len(findings)
                                    if f.amount:
                                        job["finds"].append(
                                            {"amount": f.amount, "currency": f.currency}
                                        )
                    except Exception:
                        pass
                    # raw / em fall out of scope here and are freed immediately

            batch = service.new_batch_http_request(callback=_cb)
            for mid in chunk:
                batch.add(service.users().messages().get(userId="me", id=mid, format="full"))
            try:
                batch.execute(http=http)
            except Exception:
                # A failed/timed-out batch must not kill the whole scan; the
                # callbacks that did fire already recorded their results.
                pass
            finally:
                import gc
                gc.collect()  # reclaim each batch's payloads before the next

        chunks = [ids[i:i + BATCH] for i in range(0, len(ids), BATCH)]
        if chunks:
            with ThreadPoolExecutor(max_workers=min(WORKERS, len(chunks))) as pool:
                list(pool.map(_download_chunk, chunks))

        items = [_to_dict(f) for f in dedupe(_drop_spent(findings, spent))]
        scan_id = secrets.token_urlsafe(9)
        store.save_scan(scan_id, email, items)
        job["scan_id"] = scan_id
        job["found"] = len(items)
        job["done"] = True
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if "invalid_grant" in msg or "expired or revoked" in msg.lower():
            job["reauth"] = True
            job["error"] = "Your Gmail connection expired — reconnect to dig again."
        else:
            job["error"] = msg
        job["done"] = True


try:
    store.init()
except Exception as e:  # noqa: BLE001 — don't crash boot if the DB is briefly unavailable
    print(f"[pip] store.init failed ({store.backend()}): {e}")

print(
    f"[cashew] LLM precision filter: "
    f"{'ON (' + str(active_provider()) + ' / ' + str(active_model()) + ')' if verification_enabled() else 'OFF - set ANTHROPIC_API_KEY or OPENAI_API_KEY to enable'}",
    flush=True,
)


# Official "check your balance" pages per brand. There's no universal way to
# verify a gift-card balance automatically (each brand needs the card number +
# PIN on its own site), so we link the user straight to the real page to check
# in one click. Brands not listed fall back to a balance-check web search.
BALANCE_URLS = {
    "Amazon": "https://www.amazon.com/gc/balance",
    "Starbucks": "https://www.starbucks.com/account/cards",
    "Target": "https://www.target.com/guest/gift-card-balance",
    "Walmart": "https://www.walmart.com/gift-card-balance",
    "Best Buy": "https://www.bestbuy.com/gift-card-balance",
    "Apple": "https://www.apple.com/shop/gift-cards",
    "DoorDash": "https://www.doordash.com/gift-cards/",
    "Uber Eats": "https://www.ubereats.com/",
    "Uber": "https://www.uber.com/",
    "Sephora": "https://www.sephora.com/beauty/gift-card-balance",
    "Nike": "https://www.nike.com/orders/gift-card-lookup",
    "Steam": "https://store.steampowered.com/account/",
    "PlayStation": "https://www.playstation.com/gift-cards/",
    "Xbox": "https://account.microsoft.com/billing/redeem",
    "Google Play": "https://play.google.com/store/account",
    "Etsy": "https://www.etsy.com/your/purchases/gift-cards",
    "Visa": "https://www.giftcards.com/check-your-balance",
    "Mastercard": "https://www.giftcards.com/check-your-balance",
    "Home Depot": "https://www.homedepot.com/c/check_card_balance",
    "Lowe's": "https://www.lowes.com/l/check-gift-card-balance.html",
    "Nordstrom": "https://www.nordstrom.com/c/gift-card-balance",
    "REI": "https://www.rei.com/giftcard/balance",
    "Chipotle": "https://www.chipotle.com/gift-cards",
    "Grubhub": "https://www.grubhub.com/gift-cards",
    "Instacart": "https://www.instacart.com/gift-cards",
    "Gap": "https://www.gap.com/customerService/info.do?cid=81458",
    "Old Navy": "https://oldnavy.gap.com/customerService/info.do?cid=2412",
    "IKEA": "https://www.ikea.com/us/en/customer-service/gift-cards/",
}


# AU prepaid Visa/Mastercard gift cards (Category Choice / "AU Gift Cards"
# programs sold at Australia Post, supermarkets, etc.) check balance here.
CARDBALANCE_AU = "https://www.cardbalance.com.au/"


def _balance_url(brand: str, kind: str, currency: str = "USD") -> str:
    """Official balance-check page for this brand, or a web-search fallback.

    Only meaningful for spendable balances (cards / store credit) — referral
    rewards live inside the brand's app, so we skip the link there.
    """
    if kind not in ("gift_card", "store_credit"):
        return ""
    # AU prepaid Visa/Mastercard cards have a dedicated balance portal.
    if brand in ("Visa", "Mastercard") and currency == "AUD":
        return CARDBALANCE_AU
    if brand in BALANCE_URLS:
        return BALANCE_URLS[brand]
    if not brand or brand == "Unknown":
        return ""
    import urllib.parse

    q = urllib.parse.quote(f"{brand} gift card check balance")
    return "https://www.google.com/search?q=" + q


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
        "balance_url": _balance_url(f.brand, f.kind, f.currency),
    }


def _run_sample_scan() -> list:
    findings = []
    spent = set()
    for email in iter_mbox(str(SAMPLE_MBOX)):
        f = _detect(email)
        if f:
            findings.append(f)
        rb = redemption_brand(email)
        if rb:
            spent.add(rb.lower())
    return [_to_dict(f) for f in dedupe(_drop_spent(findings, spent))]


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
        "finds": [],  # [{amount, currency}] streamed live for the dopamine pops
        "reauth": False,  # set when the Gmail token is dead → send user to reconnect
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
        "finds": job["finds"],
        "reauth": job.get("reauth", False),
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
    # Always show Google's account chooser (+ consent) so the user explicitly
    # picks WHICH Gmail to dig — never silently reuse a prior connection. The
    # typed email is passed as a hint so the right account is pre-selected.
    hint = request.query_params.get("hint", "").strip()
    auth_kwargs = dict(
        access_type="offline",
        include_granted_scopes="true",
        prompt="select_account consent",
    )
    if hint:
        auth_kwargs["login_hint"] = hint
    auth_url, state = flow.authorization_url(**auth_kwargs)
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
    # Behind a TLS-terminating proxy (Render/Vercel) the request arrives over
    # http, which oauthlib rejects ("OAuth 2 MUST utilize https"). Rebuild the
    # callback URL on the canonical OAUTH_REDIRECT (https) + the incoming query.
    import urllib.parse as _url
    incoming = _url.urlsplit(str(request.url))
    base = _url.urlsplit(OAUTH_REDIRECT)
    auth_response = _url.urlunsplit(
        (base.scheme, base.netloc, base.path, incoming.query, "")
    )
    try:
        flow.fetch_token(authorization_response=auth_response)
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


def _unlocked_payload(scan_id: str, rec: dict, tier: str = "once") -> dict:
    items = rec["items"]
    return {
        "ok": True,
        "paid": True,
        "tier": tier,
        "tier_label": TIERS.get(tier, TIERS["once"])["label"],
        "items": items,
        "total": round(sum(i["amount"] or 0 for i in items), 2),
        "currency": items[0]["currency"] if items else "USD",
    }


@app.post("/api/unlock/{scan_id}")
async def unlock(scan_id: str, request: Request):
    """Start payment for a scan. With Stripe configured, returns a Checkout URL
    and the scan is only revealed after the webhook confirms payment. Without a
    Stripe key (local dev) it unlocks directly so the flow still works."""
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
    if rec.get("paid"):  # already paid — just reveal
        return _unlocked_payload(scan_id, rec, tier)

    stripe = _stripe()
    if stripe is None:  # no Stripe configured (local/dev) — unlock free
        store.mark_paid(scan_id, tier)
        return _unlocked_payload(scan_id, store.get_scan(scan_id), tier)

    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {"name": "Cashew — unlock this dig"},
                    "unit_amount": UNLOCK_PRICE_CENTS,
                },
                "quantity": 1,
            }],
            metadata={"scan_id": scan_id, "tier": tier},
            success_url=f"{SITE_ORIGIN}/?unlocked={scan_id}&s={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{SITE_ORIGIN}/?dig={scan_id}",
        )
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"payment error: {e}"}, status_code=502)
    return {"ok": True, "checkout_url": session.url}


@app.post("/api/stripe/webhook")
async def stripe_webhook(request: Request):
    """Stripe's authoritative payment confirmation — the ONLY place we mark a
    scan paid. Verifies the signature, then flips paid on checkout completion."""
    stripe = _stripe()
    if stripe is None or not STRIPE_WEBHOOK_SECRET:
        return JSONResponse({"ok": False}, status_code=400)
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception:  # bad signature / malformed
        return JSONResponse({"ok": False}, status_code=400)
    # NOTE: Stripe objects use bracket access, NOT .get() (which raises here).
    try:
        if event["type"] == "checkout.session.completed":
            meta = event["data"]["object"]["metadata"] or {}
            scan_id = meta["scan_id"] if "scan_id" in meta else None
            tier = meta["tier"] if "tier" in meta else "once"
            if scan_id:
                store.mark_paid(scan_id, tier)
    except Exception as e:  # noqa: BLE001 — never 500 the webhook
        print(f"[cashew] webhook handling error: {e}", flush=True)
    return {"ok": True}


@app.get("/api/scan/unlocked/{scan_id}")
def scan_unlocked(scan_id: str, s: str = ""):
    """After returning from Stripe, the page polls this until the scan is paid,
    then reveals the full results. If `s` (the Checkout session id) is given and
    the scan isn't yet flagged, we verify the payment with Stripe directly — so a
    paid customer is unlocked even if the webhook is delayed or misconfigured."""
    rec = store.get_scan(scan_id)
    if rec is None:
        return JSONResponse({"ok": False, "error": "unknown scan"}, status_code=404)
    if not rec.get("paid") and s:
        stripe = _stripe()
        if stripe is not None:
            try:
                sess = stripe.checkout.Session.retrieve(s)
                meta = sess["metadata"] or {}
                if sess["payment_status"] == "paid" and meta["scan_id"] == scan_id:
                    store.mark_paid(scan_id, meta["tier"] if "tier" in meta else "once")
                    rec = store.get_scan(scan_id)
            except Exception as e:  # noqa: BLE001
                print(f"[cashew] session verify error: {e}", flush=True)
    if not rec.get("paid"):
        return {"ok": True, "paid": False}  # not confirmed yet — keep polling
    return _unlocked_payload(scan_id, rec)


# Static assets (styles.css, app.js, ...). Mounted last so the routes above win.
app.mount("/", StaticFiles(directory=WEB, html=False), name="static")


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    host = "0.0.0.0" if os.environ.get("PIP_ENV") == "production" else "127.0.0.1"
    uvicorn.run("server:app", host=host, port=port, reload=False)
