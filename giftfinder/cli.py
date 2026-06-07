"""Command-line entry point.

  python -m giftfinder                 # runs the bundled demo inbox
  python -m giftfinder --mbox mail.mbox
  python -m giftfinder --imap --user you@gmail.com --since 2023/01/01
"""
import argparse
import getpass
import json
import os
import sys
from pathlib import Path
from typing import List

from .detect import detect
from .models import Finding
from .report import dedupe, render, totals_by_currency
from .sources import iter_imap, iter_mbox

DEMO_MBOX = Path(__file__).parent / "sample" / "demo.mbox"

PRIVACY_BANNER = (
    "GiftFinder — runs 100% on your machine. No email content, amounts, or "
    "gift-card codes are ever uploaded. Codes are never even stored."
)


def _scan(emails, min_confidence: float):
    findings: List[Finding] = []
    scanned = 0
    for e in emails:
        scanned += 1
        f = detect(e, min_confidence=min_confidence)
        if f:
            findings.append(f)
    return findings, scanned


def _emit_json(findings: List[Finding], scanned: int) -> None:
    findings = dedupe(findings)
    out = {
        "scanned": scanned,
        "found": len(findings),
        "totals": totals_by_currency(findings),
        "items": [
            {
                "kind": f.kind,
                "brand": f.brand,
                "amount": f.amount,
                "currency": f.currency,
                "confidence": f.confidence,
                "subject": f.subject,
                "code_present": f.code_present,   # never the code itself
                "expires_text": f.expires_text,
            }
            for f in findings
        ],
    }
    print(json.dumps(out, indent=2))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="giftfinder",
        description="Find forgotten gift cards, store credits and rewards in your inbox.",
    )
    src = p.add_mutually_exclusive_group()
    src.add_argument("--mbox", metavar="PATH", help="scan a local .mbox file (e.g. Google Takeout)")
    src.add_argument("--imap", action="store_true", help="scan Gmail live over read-only IMAP")
    p.add_argument("--user", help="Gmail address (with --imap)")
    p.add_argument("--since", help="only mail after this date, Gmail style YYYY/MM/DD")
    p.add_argument("--limit", type=int, default=500, help="max messages to fetch (--imap)")
    p.add_argument("--folder", default="[Gmail]/All Mail", help="IMAP folder (--imap)")
    p.add_argument("--min-confidence", type=float, default=0.45, help="0..1 detection threshold")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    args = p.parse_args(argv)

    quiet = args.json
    if not quiet:
        print(PRIVACY_BANNER, file=sys.stderr)

    try:
        if args.imap:
            user = args.user or input("Gmail address: ").strip()
            password = os.environ.get("GMAIL_APP_PASSWORD") or getpass.getpass(
                "Gmail app password (input hidden): "
            )
            emails = iter_imap(
                user, password, folder=args.folder, since=args.since, limit=args.limit
            )
        elif args.mbox:
            emails = iter_mbox(args.mbox)
        else:
            if not quiet:
                print(f"No source given — scanning bundled demo inbox.\n", file=sys.stderr)
            emails = iter_mbox(str(DEMO_MBOX))

        findings, scanned = _scan(emails, args.min_confidence)
    except FileNotFoundError as e:
        print(f"error: file not found: {e.filename}", file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001 — surface a friendly message
        print(f"error: {e}", file=sys.stderr)
        return 1

    if args.json:
        _emit_json(findings, scanned)
    else:
        render(findings, scanned)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
