"""Scripted eval runner for tests/eval/test_set.json.

Hits the live /ask endpoint once per case and scores the response against
that case's expected_question_type, expected_confidence, must_contain, and
must_not_contain. Requires a running API (`uvicorn app.main:app --reload`)
backed by a real GROQ_API_KEY -- this is exactly why the eval set isn't
part of the automated pytest suite (see CLAUDE.md, "Testing & evaluation").

expected_confidence is checked as a MINIMUM floor against the response's
own `confidence` field. The classifier's internal classification
confidence (app.classifier.ClassificationResult.confidence) is never
returned by /ask, so it can't be checked from outside the pipeline -- the
response's answer-confidence is the only value a live run can observe.

must_contain / must_not_contain are checked case-insensitively against
`answer + " " + explanation` combined, since response_parser can place a
given sentence (e.g. the budget_recommend disclaimer) in either field.

Usage:
    python tests/eval/run_eval.py
    python tests/eval/run_eval.py --api-base-url http://localhost:8000 -v
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import jwt
import requests

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

_REPO_ROOT = Path(__file__).resolve().parents[2]
_EVAL_SET_PATH = Path(__file__).resolve().parent / "test_set.json"
_TRANSACTIONS_PATH = _REPO_ROOT / "data" / "synthetic_transactions.json"
_BUDGETS_PATH = _REPO_ROOT / "data" / "synthetic_budgets.json"

_DEFAULT_API_BASE_URL = "http://localhost:8000"
_DEFAULT_USER_ID = "user_001"

# Demo/eval-only: signed locally with whatever JWT_SECRET the API process
# itself reads (falling back to the same "dev-only-change-me" default as
# app.main.Settings) -- not a real login flow, just enough to call /ask.
_JWT_SECRET = os.environ.get("JWT_SECRET", "dev-only-change-me")
_JWT_TIER = "premium"


def _load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _token(user_id: str) -> str:
    return jwt.encode({"sub": user_id, "tier": _JWT_TIER}, _JWT_SECRET, algorithm="HS256")


def _call_ask(
    api_base_url: str,
    user_id: str,
    question: str,
    transactions: list[dict[str, Any]],
    budgets: list[dict[str, Any]],
) -> dict[str, Any]:
    response = requests.post(
        f"{api_base_url}/ask",
        headers={"Authorization": f"Bearer {_token(user_id)}"},
        json={"user_id": user_id, "question": question, "transactions": transactions, "budgets": budgets},
        timeout=60,
    )
    response.raise_for_status()
    return response.json()


def _score_case(case: dict[str, Any], result: dict[str, Any]) -> list[str]:
    """Return a list of failure reasons; an empty list means the case passed."""
    failures: list[str] = []
    haystack = f"{result.get('answer', '')} {result.get('explanation', '')}".lower()

    actual_type = result.get("question_type")
    if actual_type != case["expected_question_type"]:
        failures.append(f"question_type: expected {case['expected_question_type']!r}, got {actual_type!r}")

    actual_confidence = result.get("confidence", 0.0)
    if actual_confidence < case["expected_confidence"]:
        failures.append(f"confidence: expected >= {case['expected_confidence']}, got {actual_confidence}")

    for term in case.get("must_contain", []):
        if term.lower() not in haystack:
            failures.append(f"must_contain: missing {term!r}")

    for term in case.get("must_not_contain", []):
        if term.lower() in haystack:
            failures.append(f"must_not_contain: leaked {term!r}")

    return failures


def run(api_base_url: str, user_id: str, verbose: bool) -> int:
    eval_set = _load_json(_EVAL_SET_PATH)
    transactions = _load_json(_TRANSACTIONS_PATH)["transactions"]
    budgets = _load_json(_BUDGETS_PATH)["budgets"]
    cases = eval_set["cases"]

    passed = 0
    category_totals: dict[str, list[int]] = defaultdict(lambda: [0, 0])  # [passed, total]

    for case in cases:
        category_totals[case["expected_question_type"]][1] += 1
        try:
            result = _call_ask(api_base_url, user_id, case["question"], transactions, budgets)
            failures = _score_case(case, result)
        except requests.RequestException as exc:
            failures = [f"request failed: {exc}"]

        status = "PASS" if not failures else "FAIL"
        if not failures:
            passed += 1
            category_totals[case["expected_question_type"]][0] += 1

        print(f"[{status}] {case['id']}: {case['question']}")
        if failures and verbose:
            for failure in failures:
                print(f"         - {failure}")

    total = len(cases)
    print("\nBy category:")
    for category, (cat_passed, cat_total) in sorted(category_totals.items()):
        print(f"  {category}: {cat_passed}/{cat_total}")

    print(f"\n{passed}/{total} passed ({passed / total:.0%})" if total else "No cases found.")
    return 0 if passed == total else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api-base-url", default=_DEFAULT_API_BASE_URL, help="Base URL of the running API.")
    parser.add_argument("--user-id", default=_DEFAULT_USER_ID, help="user_id to send with every request.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Print failure reasons under each failing case.")
    args = parser.parse_args()

    sys.exit(run(args.api_base_url, args.user_id, args.verbose))


if __name__ == "__main__":
    main()
