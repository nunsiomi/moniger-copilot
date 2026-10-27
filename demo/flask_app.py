"""Flask frontend for Moniger Copilot — replaces the old Streamlit demo.

A thin PIN-gated shell in front of the FastAPI backend's POST /ask
(app/main.py). The FastAPI app is not modified by anything here: /ask still
takes only {user_id, question} and reads transactions/budgets server-side
from the Mongo mirror (app/data_access.py).

Uploads therefore work by writing into that mirror, not into /ask: the first
upload clones the demo user's data under a throwaway per-session user token
(`usr_anon_sess<hex>`), then replaces whichever kind (transactions or
budgets) was uploaded. From then on /chat asks as that session user. Every
upload goes through app.mirror_validation first, the same fail-closed checks
the API applies on read, so contaminated or mis-shaped data is rejected here
instead of poisoning a later answer. The session user's rows are deleted on
/logout; rows from sessions that simply expire are not (see README note).

Run locally:      python demo/flask_app.py
Run in production: gunicorn demo.flask_app:app
"""
from __future__ import annotations

import csv
import hmac
import io
import json
import os
import re
import secrets
import sys
import threading
import time
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
# `python demo/flask_app.py` only puts demo/ on sys.path; the app package
# lives at the repo root.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import jwt
import requests
from bson import json_util
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, session, url_for

load_dotenv(REPO_ROOT / ".env")

# Private helpers reused on purpose: uploads must be stored and validated
# exactly the way the API will later read them, and duplicating the Mongo
# connection settings or the document decoding here would let the two drift.
from app.data_access import (  # noqa: E402
    _BUDGETS_COLLECTION,
    _TRANSACTIONS_COLLECTION,
    _db,
    _decode_document,
)
from app.mirror_validation import (  # noqa: E402
    MirrorValidationError,
    validate_budgets,
    validate_transactions,
)

# --- Configuration ---------------------------------------------------------

API_URL = os.environ.get("API_URL", "http://localhost:8000/ask")
# Demo-only token minting, same approach as the old Streamlit demo: signed
# with whatever JWT_SECRET the API process reads. The token never leaves this
# process; the browser only ever holds the signed Flask session cookie.
JWT_SECRET = os.environ.get("JWT_SECRET", "dev-only-change-me")
# The API's internal `tester` tier (TESTER_MONTHLY_QUOTA) keeps evaluation
# runs clear of the 50/month premium quota; set DEMO_JWT_TIER=premium to see
# exactly what a paying user would.
JWT_TIER = os.environ.get("DEMO_JWT_TIER", "tester")
# Whose (synthetic) data answers questions until another persona is picked
# or something is uploaded.
DEMO_USER_ID = os.environ.get("DEMO_USER_ID", "usr_anon_i07sal")

# The 20 fixture personas (moniger_copilot_fixtures_v2/README.md), selectable
# in the UI so testers can ask persona-specific questions.
PERSONAS: list[dict[str, str]] = [
    {"id": "usr_anon_t01wbl", "label": "01 Well below tax threshold", "hint": "~₦384k/yr income, tax exempt"},
    {"id": "usr_anon_t02jbl", "label": "02 Just below threshold", "hint": "₦768k/yr, exempt"},
    {"id": "usr_anon_t03jab", "label": "03 Just above threshold", "hint": "₦840k/yr, liable"},
    {"id": "usr_anon_t04cab", "label": "04 Comfortably above", "hint": "₦5.04m/yr, liable"},
    {"id": "usr_anon_t05rse", "label": "05 Mid-year raise", "hint": "₦120k to ₦480k/month from April"},
    {"id": "usr_anon_t06usd", "label": "06 USD + NGN income", "hint": "~₦12.4m/yr, worldwide-income rule"},
    {"id": "usr_anon_i07sal", "label": "07 Clean monthly salary", "hint": "₦350k salary, baseline"},
    {"id": "usr_anon_i08frl", "label": "08 Irregular freelance", "hint": "Uneven income, no budget"},
    {"id": "usr_anon_i09sid", "label": "09 Salary + side hustle", "hint": "Multiple income streams"},
    {"id": "usr_anon_i10gap", "label": "10 Income gap", "hint": "No income Apr-May"},
    {"id": "usr_anon_b11sea", "label": "11 Strong seasonality", "hint": "Spending spikes in some months"},
    {"id": "usr_anon_b12mch", "label": "12 Merchant-concentrated", "hint": "Most groceries at Shoprite"},
    {"id": "usr_anon_b13sub", "label": "13 Subscription-heavy", "hint": "6 recurring subscriptions"},
    {"id": "usr_anon_b14bet", "label": "14 Betting-heavy", "hint": "Frequent betting, no budget"},
    {"id": "usr_anon_g15par", "label": "15 Partial budgets", "hint": "Only 2 of 6 categories budgeted"},
    {"id": "usr_anon_g16trk", "label": "16 All budgets on track", "hint": "46-52% used"},
    {"id": "usr_anon_g17rsk", "label": "17 At-risk budgets", "hint": "77-79% used, ahead of pace"},
    {"id": "usr_anon_g18ovr", "label": "18 Chronically over budget", "hint": "109-120% used"},
    {"id": "usr_anon_g19rnt", "label": "19 Rent-heavy", "hint": "Housing budget 95% used"},
    {"id": "usr_anon_s20new", "label": "20 New user", "hint": "3 transactions, no budget"},
]
_PERSONA_IDS = {p["id"] for p in PERSONAS}

_PIN_RE = re.compile(r"\d{4}")
MAX_ATTEMPTS = 5
LOCKOUT_SECONDS = 5 * 60
MAX_QUESTION_CHARS = 1000
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_UPLOAD_ROWS = 20_000
SESSION_USER_PREFIX = "usr_anon_sess"


def _require_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"{name} must be set (see .env.example).")
    return value


DEMO_PIN = _require_env("DEMO_PIN")
if not _PIN_RE.fullmatch(DEMO_PIN):
    raise RuntimeError("DEMO_PIN must be exactly 4 digits.")

app = Flask(__name__)
app.secret_key = _require_env("FLASK_SECRET_KEY")
app.config.update(
    MAX_CONTENT_LENGTH=MAX_UPLOAD_BYTES,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("FLASK_COOKIE_SECURE", "").lower() == "true",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
)

if os.environ.get("TRUST_PROXY", "").lower() == "true":
    # Behind Railway/nginx request.remote_addr is the proxy, which would make
    # the lockout apply to every visitor at once. Only enable when a proxy
    # you control sets X-Forwarded-For.
    from werkzeug.middleware.proxy_fix import ProxyFix

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)  # type: ignore[method-assign]


# --- PIN lockout -------------------------------------------------------------
# In-memory and per process: with several gunicorn workers each keeps its own
# counter, so the effective limit is 5 x workers. Fine for a demo gate; use
# one worker (or Redis) if the limit must be exact.

_attempts: dict[str, dict[str, float]] = {}
_attempts_lock = threading.Lock()


def _locked_seconds(ip: str) -> int:
    """Seconds of lockout remaining for `ip` (0 if not locked)."""
    with _attempts_lock:
        entry = _attempts.get(ip)
        if not entry:
            return 0
        remaining = entry.get("locked_until", 0) - time.time()
        if remaining > 0:
            return int(remaining) + 1
        if entry.get("locked_until"):
            del _attempts[ip]  # lockout served; start fresh
        return 0


def _record_failure(ip: str) -> int:
    """Count a wrong attempt; return lockout seconds if it triggered one."""
    with _attempts_lock:
        entry = _attempts.setdefault(ip, {"fails": 0, "locked_until": 0})
        entry["fails"] += 1
        if entry["fails"] >= MAX_ATTEMPTS:
            entry["locked_until"] = time.time() + LOCKOUT_SECONDS
            entry["fails"] = 0
            return LOCKOUT_SECONDS
        return 0


def _clear_failures(ip: str) -> None:
    with _attempts_lock:
        _attempts.pop(ip, None)


def _lockout_message(seconds: int) -> str:
    minutes = max(1, -(-seconds // 60))
    return f"Too many wrong attempts. Try again in {minutes} minute{'s' if minutes != 1 else ''}."


# --- Session guard -----------------------------------------------------------


def login_required(api: bool = False) -> Callable:
    """Require a logged-in session. `api=True` routes (fetch targets) get a
    401 JSON body instead of a redirect the browser would follow silently."""

    def decorator(view: Callable) -> Callable:
        @wraps(view)
        def wrapped(*args: Any, **kwargs: Any):
            if not session.get("auth"):
                if api:
                    return jsonify(error="ERR_AUTH", message="Session expired. Please sign in again."), 401
                return redirect(url_for("login"))
            return view(*args, **kwargs)

        return wrapped

    return decorator


def _active_persona() -> str:
    persona = session.get("persona")
    return persona if persona in _PERSONA_IDS or persona == DEMO_USER_ID else DEMO_USER_ID


def _active_user_id() -> str:
    return session.get("data_user") or _active_persona()


@app.after_request
def _no_store(response):
    # Keep chat pages and API JSON out of shared/browser caches.
    if request.endpoint != "static":
        response.headers["Cache-Control"] = "no-store"
    return response


# --- Auth routes -------------------------------------------------------------


@app.get("/login")
def login():
    if session.get("auth"):
        return redirect(url_for("index"))
    ip = request.remote_addr or "unknown"
    locked = _locked_seconds(ip)
    return render_template("login.html", error=_lockout_message(locked) if locked else None)


@app.post("/login")
def login_submit():
    ip = request.remote_addr or "unknown"
    locked = _locked_seconds(ip)
    if locked:
        return render_template("login.html", error=_lockout_message(locked)), 429

    pin = request.form.get("pin", "")
    # Compare in constant time, and always compare so a malformed PIN is not
    # distinguishable by timing.
    candidate = pin if _PIN_RE.fullmatch(pin) else "0000"
    matches = hmac.compare_digest(candidate.encode(), DEMO_PIN.encode())
    if _PIN_RE.fullmatch(pin) and matches:
        _clear_failures(ip)
        session.clear()  # fresh session on privilege change
        session.permanent = True
        session["auth"] = True
        return redirect(url_for("index"))

    locked = _record_failure(ip)
    if locked:
        return render_template("login.html", error=_lockout_message(locked)), 429
    return render_template("login.html", error="Incorrect PIN."), 401


@app.get("/logout")
@login_required()
def logout():
    sid = session.get("data_user")
    if sid:
        _delete_session_data(sid)
    session.clear()
    return redirect(url_for("login"))


# --- Chat --------------------------------------------------------------------


@app.get("/")
@login_required()
def index():
    return render_template(
        "chat.html",
        using_upload=bool(session.get("data_user")),
        personas=PERSONAS,
        active_persona=_active_persona(),
    )


@app.post("/persona")
@login_required(api=True)
def select_persona():
    """Switch whose fixture data answers questions. Any uploaded session
    data is discarded, since it was cloned from the previous persona."""
    body = request.get_json(silent=True)
    persona = body.get("user_id") if isinstance(body, dict) else None
    if persona not in _PERSONA_IDS:
        return jsonify(error="ERR_VALIDATION", message="Unknown test user."), 422
    sid = session.pop("data_user", None)
    if sid:
        _delete_session_data(sid)
    session["persona"] = persona
    label = next(p["label"] for p in PERSONAS if p["id"] == persona)
    return jsonify(ok=True, user_id=persona, label=label)


@app.post("/chat")
@login_required(api=True)
def chat():
    body = request.get_json(silent=True)
    question = body.get("question") if isinstance(body, dict) else None
    if not isinstance(question, str) or not question.strip():
        return jsonify(error="ERR_VALIDATION", message="Please enter a question."), 422
    question = question.strip()
    if len(question) > MAX_QUESTION_CHARS:
        return jsonify(error="ERR_VALIDATION", message=f"Questions are limited to {MAX_QUESTION_CHARS} characters."), 422

    user_id = _active_user_id()
    token = jwt.encode({"sub": user_id, "tier": JWT_TIER}, JWT_SECRET, algorithm="HS256")
    try:
        upstream = requests.post(
            API_URL,
            headers={"Authorization": f"Bearer {token}"},
            json={"user_id": user_id, "question": question},
            timeout=60,
        )
    except requests.RequestException:
        return jsonify(error="ERR_CONNECTION", message="Couldn't reach the Copilot API. Is it running?"), 502

    try:
        payload = upstream.json()
    except ValueError:
        return jsonify(error="ERR_INTERNAL", message="The Copilot API returned an unreadable response."), 502
    return jsonify(payload), upstream.status_code


# --- Upload ------------------------------------------------------------------

_DATE_FIELDS = {"date", "budgetMonth", "periodStart", "periodEnd"}
_BOOL_FIELDS = {"fiIsRecurring", "isBillPayment", "isWalletToWallet", "isExternalTransfer"}


class UploadError(ValueError):
    """A user-facing upload problem; the message is safe to show."""


def _to_datetime(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            raise UploadError("A date value could not be read. Use ISO format, e.g. 2025-08-02T12:26:00Z.") from None
    return value


def _parse_csv(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in csv.DictReader(io.StringIO(text)):
        doc: dict[str, Any] = {}
        for key, cell in raw.items():
            if key is None or cell is None or cell.strip() == "":
                continue
            key, cell = key.strip(), cell.strip()
            if key == "amountKobo":
                try:
                    doc[key] = int(cell)
                except ValueError:
                    raise UploadError("amountKobo must be a whole number of kobo.") from None
            elif key in _BOOL_FIELDS:
                doc[key] = cell.lower() in ("true", "1", "yes")
            else:
                doc[key] = cell
        rows.append(doc)
    return rows


def _parse_json(text: str) -> tuple[str | None, list[dict[str, Any]]]:
    """Return (kind hint or None, documents). Accepts a JSON array, an object
    with a `transactions` or `budgets` array, or JSON Lines."""
    try:
        data = json_util.loads(text)
    except ValueError:
        try:
            data = [json_util.loads(line) for line in text.splitlines() if line.strip()]
        except ValueError:
            raise UploadError("That file isn't valid JSON.") from None
    hint = None
    if isinstance(data, dict):
        keys = [k for k in ("transactions", "budgets") if k in data]
        if len(keys) != 1:
            raise UploadError('Use a JSON array, or an object with exactly one of "transactions" or "budgets".')
        hint, data = keys[0], data[keys[0]]
    if not isinstance(data, list) or not all(isinstance(d, dict) for d in data):
        raise UploadError("Expected a list of records.")
    return hint, data


def _detect_kind(hint: str | None, docs: list[dict[str, Any]]) -> str:
    if not docs:
        raise UploadError("The file contains no records.")
    kinds = {"transactions" if "amountKobo" in d else "budgets" if "budgetMonth" in d else "?" for d in docs}
    if len(kinds) != 1 or "?" in kinds:
        raise UploadError("Every record must be a transaction (has amountKobo) or a budget (has budgetMonth), not a mix.")
    kind = kinds.pop()
    if hint and hint != kind:
        raise UploadError(f'The "{hint}" array holds {kind} records.')
    return kind


def _mirror_as_of_date():
    """Latest transaction date among non-upload users. Uploaded dates are
    capped to this: the API's "today" is the mirror-wide latest date, so an
    uploaded future date would shift budget maths for every other user."""
    latest = _db()[_TRANSACTIONS_COLLECTION].find_one(
        {"status": "success", "userToken": {"$not": re.compile(f"^{SESSION_USER_PREFIX}")}},
        sort=[("date", -1)],
        projection={"date": 1},
    )
    if latest is None:
        return None
    value = latest["date"]
    return value.date() if isinstance(value, datetime) else datetime.fromisoformat(str(value)[:10]).date()


def _prepare(kind: str, docs: list[dict[str, Any]], user_id: str) -> list[dict[str, Any]]:
    """Stamp the session user, normalise, and fail-closed validate."""
    if len(docs) > MAX_UPLOAD_ROWS:
        raise UploadError(f"Too many records (limit {MAX_UPLOAD_ROWS:,}).")
    prepared: list[dict[str, Any]] = []
    for doc in docs:
        doc = {k: v for k, v in doc.items() if k not in ("_id", "srcId")}
        doc["userToken"] = user_id  # the file's own token is irrelevant here
        for field in _DATE_FIELDS & doc.keys():
            doc[field] = _to_datetime(doc[field])
        if kind == "transactions":
            doc.setdefault("status", "success")
            if "date" not in doc or "direction" not in doc:
                raise UploadError("Each transaction needs at least date, direction and amountKobo.")
        prepared.append(doc)

    decoded = [_decode_document(d) for d in prepared]
    try:
        if kind == "transactions":
            validate_transactions(decoded, user_id)
        else:
            validate_budgets(decoded, user_id)
    except MirrorValidationError as exc:
        raise UploadError(f"Rejected by the privacy/schema checks: {exc}") from None

    if kind == "transactions":
        cutoff = _mirror_as_of_date()
        if cutoff is not None and any(_to_datetime(d["date"]).date() > cutoff for d in prepared):
            raise UploadError(f"Transactions can't be dated after {cutoff.isoformat()}, the demo data's latest date.")
    return prepared


def _seed_session_user(user_id: str) -> None:
    """Copy the selected persona's data so a transactions-only (or
    budgets-only) upload still leaves the other half of the picture in place."""
    db = _db()
    source = _active_persona()
    for name in (_TRANSACTIONS_COLLECTION, _BUDGETS_COLLECTION):
        copies = []
        for doc in db[name].find({"userToken": source}):
            doc.pop("_id", None)
            doc["userToken"] = user_id
            copies.append(doc)
        if copies:
            db[name].insert_many(copies)


def _delete_session_data(user_id: str) -> None:
    if not user_id.startswith(SESSION_USER_PREFIX):
        return  # never delete anything that isn't an upload user's rows
    try:
        db = _db()
        for name in (_TRANSACTIONS_COLLECTION, _BUDGETS_COLLECTION):
            db[name].delete_many({"userToken": user_id})
    except Exception:  # noqa: BLE001 - cleanup must never block logout
        app.logger.exception("Could not delete uploaded session data")


@app.post("/upload")
@login_required(api=True)
def upload():
    file = request.files.get("file")
    if file is None or not file.filename:
        return jsonify(error="ERR_VALIDATION", message="Choose a .json or .csv file."), 422
    name = file.filename.lower()
    if not name.endswith((".json", ".jsonl", ".csv")):
        return jsonify(error="ERR_VALIDATION", message="Only .json and .csv files are supported."), 422

    try:
        try:
            text = file.read().decode("utf-8-sig")
        except UnicodeDecodeError:
            raise UploadError("The file must be UTF-8 text.") from None

        if name.endswith(".csv"):
            hint, docs = "transactions", _parse_csv(text)
        else:
            hint, docs = _parse_json(text)
        kind = _detect_kind(hint, docs)
        if name.endswith(".csv") and kind != "transactions":
            raise UploadError("CSV is supported for transactions only. Upload budgets as JSON.")

        user_id = session.get("data_user") or f"{SESSION_USER_PREFIX}{secrets.token_hex(6)}"
        prepared = _prepare(kind, docs, user_id)
    except UploadError as exc:
        return jsonify(error="ERR_UPLOAD", message=str(exc)), 422

    try:
        if not session.get("data_user"):
            _seed_session_user(user_id)
            session["data_user"] = user_id
        collection = _TRANSACTIONS_COLLECTION if kind == "transactions" else _BUDGETS_COLLECTION
        coll = _db()[collection]
        coll.delete_many({"userToken": user_id})
        coll.insert_many(prepared)
    except Exception:  # noqa: BLE001
        app.logger.exception("Upload could not be stored")
        return jsonify(error="ERR_INTERNAL", message="Couldn't store the upload. Is MongoDB running?"), 500

    return jsonify(
        ok=True,
        kind=kind,
        count=len(prepared),
        message=f"Loaded {len(prepared):,} {kind}. Questions now use your uploaded data.",
    )


@app.errorhandler(413)
def _too_large(_exc):
    return jsonify(error="ERR_UPLOAD", message=f"File too large (limit {MAX_UPLOAD_BYTES // (1024 * 1024)} MB)."), 413


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), debug=False)
