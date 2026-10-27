"""Tests for the Flask demo frontend (demo/flask_app.py). No network, Mongo or
model calls: the FastAPI hop and the Mongo collections are faked."""
from __future__ import annotations

import io
import json
import os
from datetime import date
from pathlib import Path

import jwt
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
os.environ["DEMO_PIN"] = "4821"
os.environ["FLASK_SECRET_KEY"] = "test-secret"
os.environ["JWT_SECRET"] = "test-jwt-secret"
os.environ["MERCHANT_LABELS_FILE"] = str(REPO_ROOT / "moniger_copilot_fixtures_v2" / "merchant_labels.json")

from demo import flask_app  # noqa: E402

PIN = "4821"


class FakeCollection:
    def __init__(self) -> None:
        self.docs: list[dict] = []

    def find(self, query):
        return [dict(d, _id=i) for i, d in enumerate(self.docs) if d.get("userToken") == query["userToken"]]

    def delete_many(self, query):
        self.docs = [d for d in self.docs if d.get("userToken") != query["userToken"]]

    def insert_many(self, docs):
        self.docs.extend(dict(d) for d in docs)


class FakeDb(dict):
    def __missing__(self, name):
        self[name] = FakeCollection()
        return self[name]


TXN = {
    "date": "2026-07-01T10:00:00Z", "amountKobo": 150000, "direction": "debit", "status": "success",
    "fiCategoryCode": "FND", "fiSubcategory": "Restaurants", "moneySource": "moniger",
}


@pytest.fixture()
def db(monkeypatch):
    fake = FakeDb()
    fake[flask_app._TRANSACTIONS_COLLECTION].docs.append(
        {"userToken": flask_app.DEMO_USER_ID, **TXN, "amountKobo": 999}
    )
    fake[flask_app._BUDGETS_COLLECTION].docs.append({"userToken": flask_app.DEMO_USER_ID, "budgetMonth": "2026-07-01"})
    monkeypatch.setattr(flask_app, "_db", lambda: fake)
    monkeypatch.setattr(flask_app, "_mirror_as_of_date", lambda: date(2026, 7, 20))
    return fake


@pytest.fixture()
def client():
    flask_app._attempts.clear()
    flask_app.app.config["TESTING"] = True
    return flask_app.app.test_client()


@pytest.fixture()
def authed(client):
    assert client.post("/login", data={"pin": PIN}).status_code == 302
    return client


# --- Session / PIN gate ------------------------------------------------------


def test_every_route_but_login_requires_session(client):
    assert client.get("/").headers["Location"].endswith("/login")
    assert client.get("/logout").headers["Location"].endswith("/login")
    assert client.post("/chat", json={"question": "hi"}).status_code == 401
    assert client.post("/upload").status_code == 401


def test_login_accepts_right_pin_and_rejects_wrong_or_malformed(client):
    assert client.post("/login", data={"pin": "0000"}).status_code == 401
    assert client.post("/login", data={"pin": "48"}).status_code == 401
    assert client.post("/login", data={"pin": "48a1"}).status_code == 401
    assert client.post("/login", data={"pin": "48211"}).status_code == 401
    assert client.get("/").status_code == 302  # still not signed in
    assert client.post("/login", data={"pin": PIN}).status_code == 302
    assert client.get("/").status_code == 200


def test_lockout_after_five_wrong_attempts_even_for_correct_pin(client):
    for _ in range(5):
        client.post("/login", data={"pin": "0000"})
    resp = client.post("/login", data={"pin": PIN})
    assert resp.status_code == 429
    assert b"minute" in resp.data
    assert client.get("/").status_code == 302


def test_lockout_expires(client, monkeypatch):
    for _ in range(5):
        client.post("/login", data={"pin": "0000"})
    real = flask_app.time.time()
    monkeypatch.setattr(flask_app.time, "time", lambda: real + flask_app.LOCKOUT_SECONDS + 1)
    assert client.post("/login", data={"pin": PIN}).status_code == 302


# --- Chat proxy --------------------------------------------------------------


def _fake_post(captured, status=200, payload=None):
    class Resp:
        status_code = status

        def json(self):
            return payload if payload is not None else {"answer": "ok", "explanation": "", "confidence": 0.9,
                                                        "sources": [], "question_type": "spending",
                                                        "cache_hit": False, "model_used": "m"}

    def post(url, headers, json, timeout):
        captured.update(url=url, headers=headers, json=json)
        return Resp()

    return post


def test_chat_proxies_with_server_side_jwt(authed, monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(flask_app.requests, "post", _fake_post(captured))
    resp = authed.post("/chat", json={"question": "  How much on food?  "})
    assert resp.status_code == 200 and resp.get_json()["answer"] == "ok"
    claims = jwt.decode(captured["headers"]["Authorization"].removeprefix("Bearer "), "test-jwt-secret", algorithms=["HS256"])
    assert claims["sub"] == captured["json"]["user_id"] == flask_app.DEMO_USER_ID
    assert captured["json"]["question"] == "How much on food?"
    minted = captured["headers"]["Authorization"].removeprefix("Bearer ")
    assert minted not in resp.get_data(as_text=True) and minted not in str(resp.headers)


def test_chat_passes_through_api_errors_and_validates_input(authed, monkeypatch):
    err = {"error": "ERR_QUOTA_EXCEEDED", "message": "Quota used up."}
    monkeypatch.setattr(flask_app.requests, "post", _fake_post({}, status=429, payload=err))
    resp = authed.post("/chat", json={"question": "hi"})
    assert resp.status_code == 429 and resp.get_json() == err
    assert authed.post("/chat", json={"question": "   "}).status_code == 422
    assert authed.post("/chat", json={"question": "x" * 1001}).status_code == 422
    assert authed.post("/chat", data="nope").status_code == 422


def test_chat_connection_failure_is_a_clean_502(authed, monkeypatch):
    def boom(*a, **k):
        raise flask_app.requests.ConnectionError("down")

    monkeypatch.setattr(flask_app.requests, "post", boom)
    resp = authed.post("/chat", json={"question": "hi"})
    assert resp.status_code == 502 and resp.get_json()["error"] == "ERR_CONNECTION"


# --- Upload ------------------------------------------------------------------


def _upload(client, name, content):
    return client.post("/upload", data={"file": (io.BytesIO(content.encode()), name)}, content_type="multipart/form-data")


def test_transaction_upload_replaces_data_and_chat_uses_session_user(authed, db, monkeypatch):
    resp = _upload(authed, "t.json", json.dumps([TXN, {**TXN, "amountKobo": 5000}]))
    assert resp.status_code == 200 and resp.get_json()["count"] == 2

    with authed.session_transaction() as s:
        sid = s["data_user"]
    assert sid.startswith("usr_anon_sess")
    txns = db[flask_app._TRANSACTIONS_COLLECTION].docs
    assert sorted(d["amountKobo"] for d in txns if d["userToken"] == sid) == [5000, 150000]
    # Budgets weren't uploaded, so the demo user's are cloned across.
    assert any(d["userToken"] == sid for d in db[flask_app._BUDGETS_COLLECTION].docs)
    # The demo user's own data is untouched.
    assert any(d["userToken"] == flask_app.DEMO_USER_ID and d["amountKobo"] == 999 for d in txns)

    captured: dict = {}
    monkeypatch.setattr(flask_app.requests, "post", _fake_post(captured))
    authed.post("/chat", json={"question": "hi"})
    assert captured["json"]["user_id"] == sid

    # Re-uploading replaces, not appends.
    _upload(authed, "t.json", json.dumps([TXN]))
    assert [d["amountKobo"] for d in db[flask_app._TRANSACTIONS_COLLECTION].docs if d["userToken"] == sid] == [150000]


def test_csv_transactions_and_json_budgets(authed, db):
    csv_text = "date,amountKobo,direction,fiCategoryCode,fiSubcategory,moneySource\n2026-07-02,250000,debit,TRN,Ride Hailing,moniger\n"
    assert _upload(authed, "t.csv", csv_text).status_code == 200
    budget = {"budgetMonth": "2026-07-01", "periodStart": "2026-07-01", "periodEnd": "2026-07-31", "status": "active",
              "totalMonthlyIncome": 100, "totalMonthlyExpense": 50, "spentAmount": 10, "remainingAmount": 40,
              "expenseBreakdown": [], "incomeBreakdown": []}
    resp = _upload(authed, "b.json", json.dumps({"budgets": [budget]}))
    assert resp.status_code == 200 and resp.get_json()["kind"] == "budgets"


@pytest.mark.parametrize(
    "name,content,fragment",
    [
        ("t.json", json.dumps([{**TXN, "narration": "TRANSFER TO JOHN 0123456789"}]), "Rejected"),
        ("t.json", json.dumps([{**TXN, "channel": "call 08012345678901"}]), "Rejected"),
        ("t.json", json.dumps([{**TXN, "date": "2030-01-01T00:00:00Z"}]), "dated after"),
        ("t.json", json.dumps([{**TXN, "amountKobo": 12.5}]), "Rejected"),
        ("t.json", json.dumps([{**TXN, "merchantLabel": "Some Unverified Shop"}]), "Rejected"),
        ("t.json", json.dumps([TXN, {"budgetMonth": "2026-07-01"}]), "not a mix"),
        ("t.json", "not json", "valid JSON"),
        ("t.json", "[]", "no records"),
        ("t.csv", "date,amountKobo,direction\n2026-07-01,abc,debit\n", "whole number"),
        ("t.txt", "x", "Only .json"),
    ],
)
def test_bad_uploads_are_rejected_and_store_nothing(authed, db, name, content, fragment):
    resp = _upload(authed, name, content)
    assert resp.status_code == 422 and fragment in resp.get_json()["message"]
    with authed.session_transaction() as s:
        assert "data_user" not in s
    assert len(db[flask_app._TRANSACTIONS_COLLECTION].docs) == 1  # just the seeded demo row


def test_logout_deletes_uploaded_data_but_never_demo_data(authed, db):
    _upload(authed, "t.json", json.dumps([TXN]))
    with authed.session_transaction() as s:
        sid = s["data_user"]
    assert authed.get("/logout").status_code == 302
    for name in (flask_app._TRANSACTIONS_COLLECTION, flask_app._BUDGETS_COLLECTION):
        assert not any(d["userToken"] == sid for d in db[name].docs)
    assert any(d["userToken"] == flask_app.DEMO_USER_ID for d in db[flask_app._TRANSACTIONS_COLLECTION].docs)
    flask_app._delete_session_data(flask_app.DEMO_USER_ID)  # guard: not an upload user
    assert any(d["userToken"] == flask_app.DEMO_USER_ID for d in db[flask_app._TRANSACTIONS_COLLECTION].docs)


# --- Persona selection -------------------------------------------------------


def test_persona_switch_changes_who_chat_asks_as(authed, monkeypatch):
    page = authed.get("/").get_data(as_text=True)
    assert "usr_anon_t04cab" in page and 'id="persona-select"' in page

    resp = authed.post("/persona", json={"user_id": "usr_anon_t04cab"})
    assert resp.status_code == 200 and resp.get_json()["label"] == "04 Comfortably above"

    captured: dict = {}
    monkeypatch.setattr(flask_app.requests, "post", _fake_post(captured))
    authed.post("/chat", json={"question": "How much tax would I pay?"})
    claims = jwt.decode(captured["headers"]["Authorization"].removeprefix("Bearer "), "test-jwt-secret", algorithms=["HS256"])
    assert captured["json"]["user_id"] == claims["sub"] == "usr_anon_t04cab"
    assert claims["tier"] == flask_app.JWT_TIER == "tester"


def test_persona_switch_rejects_unknown_users(authed):
    for bad in ({"user_id": "usr_anon_sessdeadbeef"}, {"user_id": "anything"}, {}):
        assert authed.post("/persona", json=bad).status_code == 422
    with authed.session_transaction() as s:
        assert "persona" not in s


def test_persona_switch_discards_uploaded_data(authed, db):
    _upload(authed, "t.json", json.dumps([TXN]))
    with authed.session_transaction() as s:
        sid = s["data_user"]
    assert authed.post("/persona", json={"user_id": "usr_anon_g18ovr"}).status_code == 200
    with authed.session_transaction() as s:
        assert "data_user" not in s and s["persona"] == "usr_anon_g18ovr"
    assert not any(d["userToken"] == sid for d in db[flask_app._TRANSACTIONS_COLLECTION].docs)


def test_upload_clones_the_selected_persona(authed, db):
    db[flask_app._BUDGETS_COLLECTION].docs.append({"userToken": "usr_anon_g16trk", "budgetMonth": "2026-06-01"})
    authed.post("/persona", json={"user_id": "usr_anon_g16trk"})
    _upload(authed, "t.json", json.dumps([TXN]))
    with authed.session_transaction() as s:
        sid = s["data_user"]
    cloned = [d["budgetMonth"] for d in db[flask_app._BUDGETS_COLLECTION].docs if d["userToken"] == sid]
    assert cloned == ["2026-06-01"]
