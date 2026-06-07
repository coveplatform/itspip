"""Vercel serverless entrypoint.

Vercel runs files in /api as functions. This exposes the FastAPI `app` (defined
in server.py at the repo root) so Vercel's Python runtime can serve it as ASGI.
"""
import os
import sys

# Make the repo root importable (server.py, store.py, giftfinder/ live there).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import app  # noqa: E402,F401
