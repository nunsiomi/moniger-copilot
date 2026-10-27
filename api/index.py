"""Vercel entry point for the PIN-gated demo UI (demo/flask_app.py).

vercel.json rewrites every path to this function. Vercel serves the WSGI
`app` object exported here; the API itself runs separately (Railway) and is
reached through API_URL.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from demo.flask_app import app  # noqa: E402,F401
