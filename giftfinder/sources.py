"""Email sources. Two ways in, both read-only:

  - mbox  : a local file (Google Takeout export, or the bundled demo). Fully
            offline — the gold standard for "we never touch your account".
  - imap  : a live read-only Gmail connection via an app password. Convenient,
            still local: messages are fetched straight to your machine and
            never sent anywhere.
"""
import email
import imaplib
import mailbox
import re
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from typing import Iterator, Optional

from .models import Email

_HTML_TAG_RE = re.compile(r"(?s)<[^>]+>")
_SCRIPT_RE = re.compile(r"(?is)<(script|style).*?</\1>")
_WS_RE = re.compile(r"\s+")

# Gmail-native search: only pull messages that could plausibly hold money.
GMAIL_QUERY = (
    '"gift card" OR "e-gift" OR egift OR "gift certificate" OR '
    '"store credit" OR "account credit" OR "travel credit" OR '
    'referral OR voucher OR "you earned" OR "reward credit"'
)


def _header_str(raw) -> str:
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return str(raw)


def _strip_html(s: str) -> str:
    import html
    s = _SCRIPT_RE.sub(" ", s)
    s = _HTML_TAG_RE.sub(" ", s)
    s = html.unescape(s)
    return _WS_RE.sub(" ", s).strip()


def _decode_part(part) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except (LookupError, TypeError):
        return payload.decode("utf-8", errors="replace")


def _message_text(msg) -> str:
    parts = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == "text/plain":
                parts.append(_decode_part(part))
            elif ctype == "text/html":
                parts.append(_strip_html(_decode_part(part)))
    else:
        body = _decode_part(msg)
        if msg.get_content_type() == "text/html":
            body = _strip_html(body)
        parts.append(body)
    return "\n".join(p for p in parts if p)


def _to_email(msg) -> Email:
    date = None
    try:
        if msg.get("Date"):
            date = parsedate_to_datetime(msg.get("Date"))
    except Exception:
        date = None
    return Email(
        subject=_header_str(msg.get("Subject")),
        sender=_header_str(msg.get("From")),
        date=date,
        body=_message_text(msg),
        message_id=_header_str(msg.get("Message-ID")) or "",
    )


def email_from_bytes(raw: bytes) -> Email:
    """Parse a raw RFC822 message (e.g. a Gmail API `raw` payload) into an Email."""
    return _to_email(email.message_from_bytes(raw))


def iter_mbox(path: str) -> Iterator[Email]:
    box = mailbox.mbox(path)
    try:
        for msg in box:
            yield _to_email(msg)
    finally:
        box.close()


def iter_imap(
    user: str,
    password: str,
    host: str = "imap.gmail.com",
    folder: str = "[Gmail]/All Mail",
    since: Optional[str] = None,
    limit: int = 500,
) -> Iterator[Email]:
    """Stream candidate messages from Gmail, read-only.

    `since` is a Gmail-style date 'YYYY/MM/DD'. `password` is a Gmail App
    Password (Google account -> Security -> App passwords), not your login.
    """
    conn = imaplib.IMAP4_SSL(host)
    try:
        conn.login(user, password)
        conn.select(folder, readonly=True)
        query = GMAIL_QUERY
        if since:
            query += f" after:{since}"
        typ, data = conn.uid("search", None, "X-GM-RAW", query)
        if typ != "OK" or not data or not data[0]:
            return
        uids = data[0].split()[-limit:]
        for uid in uids:
            typ, msgdata = conn.uid("fetch", uid, "(RFC822)")
            if typ != "OK" or not msgdata or not msgdata[0]:
                continue
            raw = msgdata[0][1]
            yield _to_email(email.message_from_bytes(raw))
    finally:
        try:
            conn.logout()
        except Exception:
            pass
