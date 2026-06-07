"""Tiny storage layer.

Uses Postgres when DATABASE_URL / POSTGRES_URL is set (serverless / production),
and local SQLite otherwise (zero-setup local dev). Same API either way.
"""
import json
import os
import tempfile
import time
from pathlib import Path

DATABASE_URL = (
    os.environ.get("POSTGRES_URL")
    or os.environ.get("DATABASE_URL")
    or ""
).strip()
USE_PG = bool(DATABASE_URL)

if USE_PG:
    import psycopg

    def _connect():
        return psycopg.connect(DATABASE_URL, autocommit=True)

    _PH = "%s"
    _REAL = "DOUBLE PRECISION"
    _INSERT_IGNORE = "INSERT INTO waitlist (email, created) VALUES (%s,%s) ON CONFLICT (email) DO NOTHING"
else:
    import sqlite3

    _BASE = Path(__file__).parent
    _DATA = Path(os.environ.get("PIP_DATA_DIR", str(_BASE / "data")))
    try:
        _DATA.mkdir(parents=True, exist_ok=True)
    except OSError:
        _DATA = Path(tempfile.gettempdir()) / "pip-data"
        _DATA.mkdir(parents=True, exist_ok=True)
    _DB = _DATA / "waitlist.db"

    def _connect():
        return sqlite3.connect(_DB)

    _PH = "?"
    _REAL = "REAL"
    _INSERT_IGNORE = "INSERT OR IGNORE INTO waitlist (email, created) VALUES (?,?)"


def backend() -> str:
    return "postgres" if USE_PG else "sqlite"


def init() -> None:
    with _connect() as c:
        cur = c.cursor()
        cur.execute(
            f"CREATE TABLE IF NOT EXISTS waitlist (email TEXT PRIMARY KEY, created {_REAL})"
        )
        cur.execute(
            "CREATE TABLE IF NOT EXISTS scans ("
            f"id TEXT PRIMARY KEY, created {_REAL}, paid INTEGER DEFAULT 0, "
            "tier TEXT, email TEXT, items TEXT)"
        )


def add_waitlist(email: str) -> int:
    with _connect() as c:
        c.cursor().execute(_INSERT_IGNORE, (email, time.time()))
    return waitlist_count()


def waitlist_count() -> int:
    with _connect() as c:
        cur = c.cursor()
        cur.execute("SELECT COUNT(*) FROM waitlist")
        return int(cur.fetchone()[0])


def save_scan(scan_id: str, email: str, items: list) -> None:
    q = (
        f"INSERT INTO scans (id, created, paid, email, items) "
        f"VALUES ({_PH},{_PH},0,{_PH},{_PH})"
    )
    with _connect() as c:
        c.cursor().execute(q, (scan_id, time.time(), email, json.dumps(items)))


def get_scan(scan_id: str):
    with _connect() as c:
        cur = c.cursor()
        cur.execute(f"SELECT items, paid FROM scans WHERE id = {_PH}", (scan_id,))
        row = cur.fetchone()
    if not row:
        return None
    return {"items": json.loads(row[0]), "paid": bool(row[1])}


def mark_paid(scan_id: str, tier: str) -> None:
    with _connect() as c:
        c.cursor().execute(
            f"UPDATE scans SET paid = 1, tier = {_PH} WHERE id = {_PH}", (tier, scan_id)
        )
