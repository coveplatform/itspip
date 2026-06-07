"""The detection engine — the actual product value.

Given a normalized Email, decide whether it contains recoverable money
(gift card, store credit, referral reward, coupon), what brand, how much,
and how confident we are. Everything here is pure string analysis; it makes
no network calls and stores no codes.
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
        "your credit", "credit has been added", "credit to your account",
        "travel credit", "ride credit", "wallet credit",
    ],
    "referral_reward": [
        "referral", "referred", "you earned", "reward credit", "loyalty reward",
        "you've earned", "youve earned", "bonus credit", "cash reward",
    ],
    "coupon": [
        "% off", "percent off", "promo code", "coupon", "voucher code",
        "discount code",
    ],
}

# Precedence: a gift card is more valuable/certain than a coupon.
KIND_ORDER = ["gift_card", "store_credit", "referral_reward", "coupon"]

KIND_BASE_CONFIDENCE = {
    "gift_card": 0.50,
    "store_credit": 0.45,
    "referral_reward": 0.35,
    "coupon": 0.20,
}

# Signals that the recipient actually *possesses* the money (vs. marketing).
POSSESSION_SIGNALS = [
    "you received", "you've received", "youve received", "sent you",
    "your gift card", "your balance", "remaining balance", "current balance",
    "redeem", "claim your", "your code", "your e-gift", "your egift",
    "added to your account", "has been added", "is waiting", "available to use",
    "your reward", "your credit",
]

# Signals it's a marketing blast, not money you hold.
MARKETING_SIGNALS = [
    "shop now", "buy a gift card", "buy gift cards", "give the gift",
    "perfect gift", "sale ends", "shop gift cards", "order a gift card",
    "gift cards make", "this holiday", "send a gift card",
]

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
EXPIRY_RE = re.compile(
    r"expir\w*(?:\s+on|\s+date|\s+by)?[:\s]+([A-Za-z0-9][A-Za-z0-9 ,/\.\-]{4,22})",
    re.IGNORECASE,
)


def _contains_any(haystack: str, needles) -> Optional[str]:
    for n in needles:
        if n in haystack:
            return n
    return None


def _detect_brand(email: Email, text_lower: str) -> str:
    # Sender first — most reliable.
    sender_l = email.sender.lower()
    for key, display in KNOWN_BRANDS.items():
        if key in sender_l:
            return display
    # Then body.
    for key, display in KNOWN_BRANDS.items():
        if key in text_lower:
            return display
    # Fallback: second-level domain of the sender address.
    m = re.search(r"@([\w.-]+)", sender_l)
    if m:
        parts = m.group(1).split(".")
        if len(parts) >= 2:
            root = parts[-2]
            return root.capitalize()
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


def detect(email: Email, min_confidence: float = 0.4) -> Optional[Finding]:
    """Return a Finding if this email holds recoverable money, else None."""
    text = f"{email.subject}\n{email.body}"
    text_lower = text.lower()

    kind = _classify(text_lower)
    if kind is None:
        # Fallback: "<brand> gift" wording without the literal word "card",
        # but only when a possession signal proves it's money you hold
        # (this is what filters out "the perfect gift" marketing).
        if ("gift" in text_lower or "e-gift" in text_lower) and _contains_any(
            text_lower, POSSESSION_SIGNALS
        ):
            kind = "gift_card"
        else:
            return None

    confidence = KIND_BASE_CONFIDENCE[kind]
    amount, currency = _extract_amount(text)
    code_present = bool(CODE_RE.search(text))
    brand = _detect_brand(email, text_lower)

    if _contains_any(text_lower, POSSESSION_SIGNALS):
        confidence += 0.25
    if amount is not None:
        confidence += 0.15
    if brand != "Unknown":
        confidence += 0.15
    if code_present:
        confidence += 0.10
    if _contains_any(text_lower, MARKETING_SIGNALS):
        confidence -= 0.25

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
