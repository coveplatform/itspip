"""The detection engine — the actual product value.

Given a normalized Email, decide whether it contains money the recipient
*actually holds* (a gift card, store credit, or reward balance), the brand,
and how much. Pure string analysis — no network calls, stores no codes.

Precision-first: we'd rather miss a stash than show a marketing dangle. An
email only counts if it has a real dollar amount AND evidence the money is
held (a balance/redeem phrase or a claim code), and isn't a "refer & earn /
spend & save" promo.
"""
import re
from typing import Optional, Tuple

from .models import Email, Finding


# --- Keyword vocabularies -------------------------------------------------

KIND_KEYWORDS = {
    "gift_card": [
        "gift card", "giftcard", "e-gift", "egift", "e gift card",
        "gift certificate", "gift voucher", "evoucher", "e-voucher",
    ],
    "store_credit": [
        "store credit", "account credit", "credit balance", "merchandise credit",
        "credit has been added", "credit to your account", "travel credit",
        "ride credit", "wallet credit", "in credit",
    ],
    "referral_reward": [
        "you earned", "you've earned", "youve earned", "reward credit",
        "bonus credit", "reward balance", "you got a reward",
    ],
}

KIND_ORDER = ["gift_card", "store_credit", "referral_reward"]

KIND_BASE_CONFIDENCE = {
    "gift_card": 0.55,
    "store_credit": 0.50,
    "referral_reward": 0.45,
}

# Evidence the recipient actually *holds* the money (vs. an offer to earn it).
POSSESSION_SIGNALS = [
    "you received", "you've received", "youve received", "sent you a",
    "your gift card", "your balance", "remaining balance", "current balance",
    "balance of", "your e-gift", "your egift", "redeem", "claim your",
    "added to your account", "has been added", "available to use", "is waiting",
    "your credit", "credit balance", "in your account", "your reward is",
    "enjoy your", "here is your", "here's your",
]

# Language that means money is being *dangled*, not held. If present without a
# genuine possession signal, we drop the email — even if it quotes a number.
MARKETING_SIGNALS = [
    "shop now", "buy a gift card", "buy gift cards", "give the gift",
    "perfect gift", "sale ends", "shop gift cards", "order a gift card",
    "gift cards make", "send a gift card", "refer a friend", "refer friends",
    "refer a ", "invite a friend", "invite friends", "invite your friends",
    "earn up to", "up to $", "up to £", "up to €", "save $", "save up to",
    "when you refer", "for every friend", "for each friend", "when you spend",
    "spend $", "% off", "percent off", "limited time", "ends soon",
    "refer and earn", "share your link", "referral link", "give $", "give and get",
    "start earning", "earn rewards", "sign up and", "get started", "promo code",
    "discount code", "coupon",
]

# If any of these appear, it isn't held consumer money — drop the email outright.
HARD_EXCLUDE = [
    # lottery / sweepstakes / phishing "WIN a $X gift card"
    "win a ", "win one", "win up to", "you could win", "chance to win",
    "enter to win", "sweepstake", "prize draw", "raffle", "you've won",
    "youve won", "lucky winner", "in the draw", "to win",
    # prepaid service / developer account balances (Twilio, OpenAI, etc.)
    "running low", "recharge", "auto-recharge", "auto reload", "top up",
    "top-up", "we charged", "bring the balance", "balance is currently",
    "add funds", "add credit", "api credit", "usage this month",
    # course / event / subscription sales (Grant Cardone, 10x, OddsJam, etc.)
    "enrol", "enroll", "masterclass", "webinar", "your seat", "payment plan",
    "early bird", "challenge starts", "cart closes", "doors close",
    "subscription", "free trial", "renew your", "your plan",
]

# Amounts right after these words are offers/discounts, not held balances.
AMOUNT_SKIP_BEFORE = ("up to", "upto", "save", "spend", "earn up to", "as much as")

VALUE_HINTS = (
    "balance", "worth", "value", "amount", "credit", "gift card", "received",
    "total of", "egift", "e-gift", "reward",
)

KNOWN_BRANDS = {
    "amazon": "Amazon", "starbucks": "Starbucks", "target": "Target",
    "walmart": "Walmart", "bestbuy": "Best Buy", "best buy": "Best Buy",
    "apple": "Apple", "itunes": "Apple", "doordash": "DoorDash",
    "uber eats": "Uber Eats", "ubereats": "Uber Eats", "uber": "Uber",
    "airbnb": "Airbnb", "sephora": "Sephora", "nike": "Nike", "steam": "Steam",
    "playstation": "PlayStation", "xbox": "Xbox", "nintendo": "Nintendo",
    "google play": "Google Play", "googleplay": "Google Play", "etsy": "Etsy",
    "visa": "Visa", "mastercard": "Mastercard", "home depot": "Home Depot",
    "lowes": "Lowe's", "chipotle": "Chipotle", "dunkin": "Dunkin'",
    "grubhub": "Grubhub", "instacart": "Instacart", "delta": "Delta",
    "southwest": "Southwest", "lyft": "Lyft", "old navy": "Old Navy",
    "gap": "Gap", "nordstrom": "Nordstrom", "rei": "REI", "ikea": "IKEA",
    "feverup": "Fever", "fever": "Fever",
}

# Domain labels that are never a brand name (TLDs + mail-infra subdomains).
PUBLIC_SUFFIX = {
    "com", "net", "org", "co", "io", "au", "uk", "us", "ca", "nz", "gov", "edu",
    "app", "dev", "info", "biz", "mail", "email", "e", "em", "news", "mg", "cms",
    "mkt", "mktg", "send", "sendgrid", "mailer", "smtp", "notifications", "no-reply",
    "noreply", "info-noreply", "marketing", "discover", "hello", "express",
}

CURRENCY = {"$": "USD", "£": "GBP", "€": "EUR"}
AMOUNT_RE = re.compile(
    r"(?P<cur>[$£€])\s?(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)"
)
CODE_RE = re.compile(
    r"\b(?:[A-Z0-9]{4,}-){2,}[A-Z0-9]{3,}\b"          # ABCD-EFGH-1234
    r"|\b(?:code|pin|claim code)\b[:\s]+[A-Z0-9]{6,}",  # code: XXXXXX
    re.IGNORECASE,
)
# Only capture an expiry when it's followed by a real date / relative phrase —
# never an arbitrary sentence fragment.
EXPIRY_RE = re.compile(
    r"expir\w*\s+(?:on\s+|by\s+|date[:\s]+|in\s+)?("
    r"today|tomorrow|soon|"
    r"in \d+ (?:hours?|days?|weeks?|months?)|"
    r"\d+ (?:hours?|days?|weeks?|months?)|"
    r"(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*|"
    r"\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?|"
    r"\d{1,2}[ -](?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*(?:[ ,-]+\d{2,4})?|"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}(?:,?\s*\d{4})?"
    r")",
    re.IGNORECASE,
)


def _contains_any(haystack: str, needles) -> Optional[str]:
    for n in needles:
        if n in haystack:
            return n
    return None


def _detect_brand(email: Email, text_lower: str) -> str:
    sender_l = email.sender.lower()
    for key, display in KNOWN_BRANDS.items():
        if key in sender_l:
            return display
    for key, display in KNOWN_BRANDS.items():
        if key in text_lower:
            return display
    # Fallback: the rightmost domain label that isn't a TLD / mail-infra token.
    m = re.search(r"@([\w.-]+)", sender_l)
    if m:
        labels = [p for p in m.group(1).split(".") if p]
        for p in reversed(labels):
            if p not in PUBLIC_SUFFIX and len(p) > 2 and not p.isdigit():
                return p[:1].upper() + p[1:]
    return "Unknown"


def _extract_amount(text: str) -> Tuple[Optional[float], str]:
    lower = text.lower()
    hint_positions = [
        m.start() for h in VALUE_HINTS for m in re.finditer(re.escape(h), lower)
    ]
    best = None
    best_score = float("-inf")
    for m in AMOUNT_RE.finditer(text):
        num = float(m.group("num").replace(",", ""))
        if num <= 0:
            continue
        # Skip amounts that are offers ("up to $100", "save $20", "spend $50").
        prefix = lower[max(0, m.start() - 14):m.start()]
        if any(w in prefix for w in AMOUNT_SKIP_BEFORE):
            continue
        cur = CURRENCY[m.group("cur")]
        dist = min((abs(m.start() - h) for h in hint_positions), default=9999)
        score = -dist
        if num > 5000:          # almost certainly an order total, not a card
            score -= 1_000_000
        if score > best_score:
            best_score = score
            best = (num, cur)
    return best if best else (None, "USD")


def _classify(text_lower: str) -> Optional[str]:
    for kind in KIND_ORDER:
        if _contains_any(text_lower, KIND_KEYWORDS[kind]):
            return kind
    return None


def detect(email: Email, min_confidence: float = 0.45) -> Optional[Finding]:
    """Return a Finding only if the email holds real, recoverable money."""
    text = f"{email.subject}\n{email.body}"
    text_lower = text.lower()

    # Outright junk: lottery/phishing, prepaid service balances, course sales.
    if _contains_any(text_lower, HARD_EXCLUDE):
        return None

    kind = _classify(text_lower)
    if kind is None:
        # "<brand> gift" wording without the literal word "card" (e.g. Uber Eats,
        # DoorDash gifts) — only when a possession phrase proves it's held.
        if "gift" in text_lower and _contains_any(text_lower, POSSESSION_SIGNALS):
            kind = "gift_card"
        else:
            return None

    amount, currency = _extract_amount(text)
    possession = bool(_contains_any(text_lower, POSSESSION_SIGNALS))
    marketing = bool(_contains_any(text_lower, MARKETING_SIGNALS))
    code_present = bool(CODE_RE.search(text))

    # --- hard gates (precision over recall) -------------------------------
    # 1) Must quote a real dollar amount. Kills all the $0 / no-value noise.
    if amount is None or amount <= 0:
        return None
    # 2) Marketing language wins unless there's a genuine "you hold it" phrase.
    #    Drops "refer & earn up to $100", "spend $50 save $10", promo codes, etc.
    if marketing and not possession:
        return None
    # 3) Need evidence the money is held: a possession phrase or a claim code.
    if not (possession or code_present):
        return None

    brand = _detect_brand(email, text_lower)

    confidence = KIND_BASE_CONFIDENCE[kind]
    if possession:
        confidence += 0.25
    if code_present:
        confidence += 0.10
    if brand != "Unknown":
        confidence += 0.10
    if marketing:
        confidence -= 0.15
    confidence = max(0.0, min(1.0, confidence))
    if confidence < min_confidence:
        return None

    exp_match = EXPIRY_RE.search(text)
    expires_text = exp_match.group(1).strip().rstrip(".,") if exp_match else None

    return Finding(
        kind=kind,
        brand=brand,
        amount=amount,
        currency=currency,
        confidence=round(confidence, 2),
        subject=email.subject,
        sender=email.sender,
        date=email.date,
        expires_text=expires_text,
        code_present=code_present,
        message_id=email.message_id,
    )
