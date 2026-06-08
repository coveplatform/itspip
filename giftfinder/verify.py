"""LLM verification — the precision upgrade over keyword matching.

`detect.py` is high-recall but brittle: "enter our raffle for a chance to win
a $300 Visa gift card" still quotes a dollar amount and the word "redeem", so
the keyword gates let it through. This module asks an LLM to make the final
call on each candidate: does the recipient ACTUALLY hold spendable money right
now, or is this a marketing dangle / sweepstakes / offer to earn?

Provider-agnostic: it uses whichever key you have set —
  - ANTHROPIC_API_KEY  -> Claude   (default model claude-opus-4-8)
  - OPENAI_API_KEY     -> OpenAI   (default model gpt-4o-mini)
Force one with CASHEW_CLASSIFIER_PROVIDER=anthropic|openai. Override the model
with CASHEW_CLASSIFIER_MODEL.

Only candidates that already passed `detect()` reach here — a handful per
inbox — so the per-scan cost stays small. If no key is set (or the SDK isn't
installed) verification is skipped and the keyword detector's verdict stands,
so the app keeps working with zero config.
"""
import json
import os
import re
from dataclasses import replace
from typing import Optional, Tuple

from .models import Email, Finding

# Bearer instruments to strip BEFORE any text is sent to the LLM — gift-card
# codes, PINs, voucher/card numbers. The classifier only needs the email's
# meaning, never the actual code, so we redact these so they never leave for a
# third party. (We still tell the model whether a code was present.)
_SECRET_RE = re.compile(
    r"\b(?:[A-Z0-9]{4,}-){2,}[A-Z0-9]{3,}\b"                       # ABCD-EFGH-1234
    r"|\b(?:code|pin|claim code|voucher|card number)\b\s*[:#]?\s*[A-Z0-9]{4,}"  # code: XXXXXX
    r"|\b\d(?:[ -]?\d){12,18}\b",                                   # 13–19 digit card numbers
    re.IGNORECASE,
)


def _redact(text: str) -> str:
    """Mask gift-card codes / PINs / card numbers so they never reach the LLM."""
    return _SECRET_RE.sub("[redacted]", text or "")

_DEFAULT_MODELS = {
    "anthropic": "claude-opus-4-8",
    "openai": "gpt-4o-mini",
}

_SYSTEM = (
    "You are the precision filter for Cashew, a tool that finds gift cards, "
    "store credit, and rewards a person ACTUALLY HOLDS and can spend right now. "
    "You are shown ONE email a keyword scanner flagged as possible money. "
    "Decide whether THIS email is itself a redeemable stash the recipient holds.\n\n"
    "THE KEY TEST — is this the money, or just a notification about money?\n"
    "Set held=true ONLY when the recipient can act on the money FROM THIS EMAIL:\n"
    "- a gift card / e-gift issued TO them that carries a redemption code, PIN, or a "
    "\"redeem / view your card\" link in this email, OR\n"
    "- store credit, a refund, or a reward balance added to THEIR OWN account.\n"
    "There must be a real, specific amount they can spend.\n\n"
    "Set held=false for anything that is merely a notification, receipt, offer, or "
    "marketing — even when it names a dollar amount:\n"
    "- PURCHASE / ORDER CONFIRMATIONS and receipts: emails confirming the recipient "
    "BOUGHT or SENT a gift card (\"your gift card purchase is complete\", \"order "
    "confirmation\", \"thanks for your purchase\", \"payment received\"). These are "
    "receipts — the redeemable card with its code arrives in a SEPARATE email.\n"
    "- a card the recipient bought or sent AS A GIFT FOR SOMEONE ELSE (a different "
    "recipient email / name). That is not the recipient's money.\n"
    "- sweepstakes / raffles / giveaways (\"enter for a chance to win a $300 gift card\")\n"
    "- offers to earn (\"refer a friend and get $20\", \"spend $50 get $10\", \"earn up to $100\")\n"
    "- promotions or sales OF gift cards (\"buy a gift card\", \"gift cards make great gifts\", \"$10 off\")\n"
    "- prepaid service / developer credit (Twilio, OpenAI, cloud balances), running-low / top-up notices\n"
    "- course / event / subscription sales, generic discount or coupon codes\n"
    "- any email where there is no code, no redeem link, and no balance on the recipient's own account — "
    "a mere mention of a gift card is not a stash.\n\n"
    "\"$X Visa/Mastercard gift card\" is almost always a sweepstakes or phishing lure "
    "unless the email clearly shows a specific card with a code was issued to this recipient.\n\n"
    "EXPIRY: you are told the email's sent date and today's date. If this is a gift card "
    "or promotional credit that states an expiry, and that expiry has clearly passed as of "
    "TODAY, set held=false — it is dead money. Resolve relative expiries (\"valid for 7 "
    "days\", \"expires end of month\") against the SENT date. Note: standard retailer gift "
    "cards usually do NOT expire, so do not drop a card with a code merely for being old — "
    "only drop on an explicit expiry that has passed.\n\n"
    "When held=true, extract the brand (the company whose money it is — for a card "
    "bought through a reseller like Zip or PayPal, the brand is the store the card "
    "spends at, e.g. Coles), the amount as a number, and the currency. When unsure "
    "whether the money is truly held and usable from this email, prefer held=false — "
    "missing a real stash is better than showing a receipt or a fake one."
)

_SCHEMA = {
    "type": "object",
    "properties": {
        "held": {"type": "boolean"},
        "kind": {
            "type": "string",
            "enum": ["gift_card", "store_credit", "referral_reward", "none"],
        },
        "brand": {"type": "string"},
        "amount": {"type": "number"},
        "currency": {"type": "string", "enum": ["USD", "GBP", "EUR"]},
        "reason": {"type": "string"},
    },
    "required": ["held", "kind", "brand", "amount", "currency", "reason"],
    "additionalProperties": False,
}

_client = None      # lazily created provider client
_provider = None    # "anthropic" | "openai"
_disabled = False   # set True after an auth failure so we stop hammering a bad key


def _pick_provider() -> Optional[str]:
    forced = os.environ.get("CASHEW_CLASSIFIER_PROVIDER", "").strip().lower()
    if forced in ("anthropic", "openai"):
        return forced
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    return None


def _model_for(provider: str) -> str:
    return os.environ.get("CASHEW_CLASSIFIER_MODEL") or _DEFAULT_MODELS[provider]


def _get_client() -> Tuple[Optional[str], object]:
    """Return (provider, client), or (None, None) if unavailable."""
    global _client, _provider
    if _client is not None:
        return _provider, _client
    provider = _pick_provider()
    if provider is None:
        return None, None
    try:
        if provider == "anthropic":
            import anthropic
            _client = anthropic.Anthropic()
        else:
            import openai
            _client = openai.OpenAI()
    except ImportError:
        return None, None
    _provider = provider
    return _provider, _client


def verification_enabled() -> bool:
    """True when we can call an LLM (a key is set and its SDK is installed)."""
    provider, client = _get_client()
    return client is not None


def active_provider() -> Optional[str]:
    provider, _ = _get_client()
    return provider


def active_model() -> Optional[str]:
    provider, _ = _get_client()
    return _model_for(provider) if provider else None


def _ask_anthropic(client, user: str) -> dict:
    resp = client.messages.create(
        model=_model_for("anthropic"),
        max_tokens=500,
        system=_SYSTEM,
        messages=[{"role": "user", "content": user}],
        output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
    )
    text = next((b.text for b in resp.content if b.type == "text"), "")
    return json.loads(text)


def _ask_openai(client, user: str) -> dict:
    resp = client.chat.completions.create(
        model=_model_for("openai"),
        messages=[
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": user},
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "verdict", "strict": True, "schema": _SCHEMA},
        },
    )
    return json.loads(resp.choices[0].message.content)


def verify_finding(email: Email, finding: Finding) -> Optional[Finding]:
    """Final precision gate over a keyword-detector candidate.

    Returns the finding (possibly with brand/amount/kind corrected by the LLM),
    or None if it judges this isn't money the recipient actually holds. If
    verification is unavailable or errors, the original finding is returned
    unchanged — we never drop real money over a transient API hiccup.
    """
    global _disabled
    if _disabled:
        return finding
    provider, client = _get_client()
    if client is None:
        return finding

    import datetime as _dt

    # Strip codes/PINs/card numbers BEFORE anything is sent to the LLM.
    body = _redact(email.body or "")[:6000]
    subject = _redact(email.subject or "")
    guess_amt = f"{finding.currency} {finding.amount}" if finding.amount is not None else "unknown"
    code_note = "yes" if finding.code_present else "no"
    sent = email.date.date().isoformat() if email.date else "(unknown)"
    today = _dt.date.today().isoformat()
    user = (
        f"The keyword scanner guessed: kind={finding.kind}, brand={finding.brand}, "
        f"amount={guess_amt}.\n"
        f"Redemption code/PIN detected in this email (keyword scan): {code_note} "
        "(the actual code has been redacted from the text below for privacy). "
        "If no code or redeem link is present and this is not credit on the recipient's "
        "own account, it is most likely a notification or receipt, not a held stash.\n"
        f"Email sent date: {sent}. Today's date: {today}.\n\n"
        "--- EMAIL ---\n"
        f"From: {email.sender}\n"
        f"To: {email.recipient or '(unknown)'}\n"
        f"Subject: {subject}\n\n"
        f"{body}\n"
        "--- END EMAIL ---"
    )

    try:
        verdict = _ask_anthropic(client, user) if provider == "anthropic" else _ask_openai(client, user)
    except Exception as e:  # noqa: BLE001 — fail open, keep the keyword verdict
        name = type(e).__name__
        status = getattr(e, "status_code", None)
        if name == "AuthenticationError" or status in (401, 403):
            _disabled = True  # bad/expired key — stop trying for the rest of this process
            print(
                f"[cashew] LLM filter DISABLED ({status or name}) — check the API key. "
                "Falling back to keyword detection for this scan.",
                flush=True,
            )
        else:
            print(f"[cashew] verify skipped ({name}): {e}", flush=True)
        return finding

    if not verdict.get("held"):
        return None

    # Trust the LLM's reading of the brand/amount/kind over the keyword heuristics.
    kind = verdict.get("kind")
    if kind not in ("gift_card", "store_credit", "referral_reward"):
        kind = finding.kind
    amount = verdict.get("amount")
    amount = float(amount) if isinstance(amount, (int, float)) and amount > 0 else finding.amount
    brand = (verdict.get("brand") or finding.brand).strip() or finding.brand
    currency = verdict.get("currency") if verdict.get("currency") in ("USD", "GBP", "EUR") else finding.currency

    return replace(
        finding,
        kind=kind,
        brand=brand,
        amount=amount,
        currency=currency,
        confidence=max(finding.confidence, 0.9),  # LLM-confirmed
    )
