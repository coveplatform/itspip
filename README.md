# GiftFinder

**Scan your inbox. Find the gift cards, store credits, and rewards you forgot you had.**

Most people have $50–$300 in gift cards, store credits, referral rewards, and
return credits buried in old emails — money they already own and will never
spend because they forgot it exists. GiftFinder digs it out.

```
You're sitting on $245.57 in forgotten money.
7 items across 10 scanned emails — all detected locally, nothing left your machine.
```

## The trust pitch (this is the whole point)

Gift-card codes are **bearer instruments** — whoever has the code has the money.
That's exactly why nobody trusts a website with their inbox full of them. So
GiftFinder doesn't ask you to.

- **Runs 100% on your machine.** No server, no upload, no account.
- **Codes are never stored** — only *whether* a code is present (a flag).
- **Read-only.** It never sends, deletes, or modifies anything.

"We never see your gift cards — it all runs on your computer" is the headline,
not the fine print.

## Try it in one command

```bash
pip install -r requirements.txt
python -m giftfinder            # scans a bundled demo inbox
```

## Scan your real inbox

**Option A — fully offline (Google Takeout).** Export your mail as `.mbox`
from [takeout.google.com](https://takeout.google.com), then:

```bash
python -m giftfinder --mbox "C:\path\to\All mail.mbox"
```

**Option B — live Gmail, read-only.** Create a Gmail *App Password*
(Google Account → Security → 2-Step Verification → App passwords — this is
**not** your login password), then:

```bash
python -m giftfinder --imap --user you@gmail.com --since 2022/01/01
```

It only pulls messages that match money-bearing search terms, fetches them
straight to your machine, and analyzes them locally.

### Options

| Flag | Meaning |
|------|---------|
| `--mbox PATH` | scan a local `.mbox` export |
| `--imap` | scan Gmail live, read-only (needs an App Password) |
| `--user` / `--since` / `--limit` / `--folder` | IMAP controls |
| `--min-confidence 0.45` | detection threshold, 0–1 |
| `--json` | machine-readable output (still never includes codes) |

## How detection works

For each email it classifies the kind (gift card / store credit / referral
reward / coupon), pulls the brand and amount, and scores confidence using
**possession signals** ("you received", "your balance", "added to your
account") versus **marketing signals** ("shop now", "the perfect gift") — so a
real $50 card scores high and a "gift cards make great gifts" blast gets
filtered out. Reminder emails about the same card are de-duplicated so the
total isn't inflated.

## Where this goes (the business)

The free scan above is the hook. The natural paid layer, given the money is
lumpy and one-time rather than recurring:

- **Ongoing monitoring** — alert the moment a new card/credit lands, before it expires.
- **One-click redeem list** — the tedious "actually go claim these" workflow.
- **Recovery cut** — take a small % of money surfaced, instead of a subscription.

## The website (freemium SaaS)

A cozy, hand-drawn landing page starring **Cashew the squirrel**, with a real
working dig → paywall → unlock funnel. No front-end frameworks, no trackers.

```bash
pip install -r requirements.txt
python server.py
# open http://127.0.0.1:8000
```

**The funnel:**
1. Enter email → Cashew "scans" (the real `giftfinder` engine runs over a bundled
   **sample inbox** so the whole thing works with zero setup).
2. Results: **one stash is revealed free**, the rest are **locked** — and the
   locked brands/codes are withheld *server-side*, so the paywall can't be
   bypassed by inspecting the page.
3. A three-tier paywall **upsells** to *Cashew Pro* (monthly monitoring), which is
   pre-selected as "most diggers pick this."
4. Unlock → every brand + how-to-claim is revealed, with coin confetti.

**Endpoints** (`server.py`):
- `POST /api/scan` — run a dig, returns the teaser (1 free + locked).
- `POST /api/unlock/{id}` — unlock a scan (mock payment; Stripe-ready).
- `POST /api/waitlist`, `GET /api/stats` — signups + counter.
- Data in `data/waitlist.db` (`waitlist` + `scans` tables).

**Real Gmail (built in):** click **Connect your Gmail** → read-only Google
OAuth → "dig my real inbox" runs the same detector over your actual mail. Set
`GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` to enable it — full steps in
[GMAIL_SETUP.md](GMAIL_SETUP.md). Without creds, the sample dig still works.

**Last swap for launch — real payments:** in `unlock()`, create a Stripe
Checkout Session and only flip `paid=1` from the Stripe webhook. Tiers/prices
live in the `TIERS` dict.

## Project layout

```
giftfinder/
  detect.py     # the detection engine (pure string analysis, no I/O)
  sources.py    # mbox + read-only IMAP inputs
  report.py     # dedupe, total, and render the shock number
  cli.py        # command-line entry point
  models.py     # Email / Finding data structures
  sample/       # bundled demo inbox
```

## Status

v0.1 — working local prototype. Detection is heuristic and tuned for US
retailers; contributions of new brand patterns and edge-case emails welcome.
