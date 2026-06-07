"""Core data structures. A Finding is one recovered piece of money."""
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class Email:
    """A normalized email message, source-agnostic (IMAP or mbox)."""
    subject: str
    sender: str
    date: Optional[datetime]
    body: str
    message_id: str
    recipient: str = ""   # the "To" header — helps spot cards sent to someone else


@dataclass
class Finding:
    """One detected pocket of money sitting in the inbox.

    Note: we deliberately never store the actual gift-card *code* — only
    whether one is present. The code is the bearer instrument; keeping it
    out of our data model is the whole trust pitch.
    """
    kind: str               # gift_card | store_credit | referral_reward | coupon
    brand: str
    amount: Optional[float]
    currency: str
    confidence: float       # 0..1
    subject: str
    sender: str
    date: Optional[datetime]
    expires_text: Optional[str]
    code_present: bool
    message_id: str

    @property
    def dedupe_key(self) -> str:
        amt = f"{self.amount:.2f}" if self.amount is not None else "?"
        return f"{self.kind}|{self.brand.lower()}|{amt}"


KIND_LABELS = {
    "gift_card": "Gift card",
    "store_credit": "Store credit",
    "referral_reward": "Referral / reward",
    "coupon": "Coupon",
}
