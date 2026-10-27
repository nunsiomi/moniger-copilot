"""Tests for scripts/run_questions.py's free-tier handling: --limit, resume,
retry-once on a rate limit, and stopping when the daily allowance is gone.
No network: `ask` and `time.sleep` are replaced."""
from __future__ import annotations

import csv
import importlib.util
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parent.parent / "scripts" / "run_questions.py"
_spec = importlib.util.spec_from_file_location("run_questions", _PATH)
run_questions = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_questions)

OK = {"status": 200, "answer": "fine", "question_type": "spending"}
LIMITED = {"status": 429, "error": "ERR_MODEL_RATE_LIMIT: slow down"}


@pytest.fixture()
def questions(tmp_path):
    path = tmp_path / "q.csv"
    path.write_text("user_id,question,expected\n" + "".join(f"usr_anon_i07sal,Q{i},E{i}\n" for i in range(5)),
                    encoding="utf-8")
    return path


def _run(monkeypatch, path, replies, *args):
    calls = []

    def fake_ask(api, secret, tier, user_id, question):
        calls.append(question)
        return dict(replies.pop(0))

    monkeypatch.setattr(run_questions, "ask", fake_ask)
    monkeypatch.setattr(run_questions.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(sys, "argv", ["run_questions.py", str(path), *args])
    assert run_questions.main() == 0
    with path.with_name("q_results.csv").open(encoding="utf-8-sig", newline="") as handle:
        return calls, list(csv.DictReader(handle))


def test_limit_then_resume_skips_answered(monkeypatch, questions):
    calls, rows = _run(monkeypatch, questions, [OK, OK], "--limit", "2")
    assert calls == ["Q0", "Q1"] and [r["question"] for r in rows] == ["Q0", "Q1"]

    calls, rows = _run(monkeypatch, questions, [OK, OK, OK])
    assert calls == ["Q2", "Q3", "Q4"]
    assert [r["question"] for r in rows] == ["Q0", "Q1", "Q2", "Q3", "Q4"]
    assert all(r["status"] == "200" for r in rows)


def test_rate_limit_is_retried_once(monkeypatch, questions):
    calls, rows = _run(monkeypatch, questions, [LIMITED, OK], "--limit", "1")
    assert calls == ["Q0", "Q0"] and rows[0]["status"] == "200"


def test_stops_after_repeated_rate_limits_and_retries_them_next_run(monkeypatch, questions):
    calls, rows = _run(monkeypatch, questions, [OK] + [LIMITED] * 6)
    assert calls == ["Q0", "Q1", "Q1", "Q2", "Q2", "Q3", "Q3"]  # stopped before Q4
    assert [r["status"] for r in rows] == ["200", "429", "429", "429"]

    calls, rows = _run(monkeypatch, questions, [OK] * 4)
    assert calls == ["Q1", "Q2", "Q3", "Q4"]  # failures are asked again, Q0 is not


def test_quota_errors_are_not_retried(monkeypatch, questions):
    quota = {"status": 429, "error": "ERR_QUOTA_EXCEEDED: monthly quota"}
    calls, _ = _run(monkeypatch, questions, [quota], "--limit", "1")
    assert calls == ["Q0"]
