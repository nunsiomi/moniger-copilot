"""Run a CSV of test questions against the Copilot API and write the answers
out for grading.

Input CSV columns: `user_id`, `question`, and optionally `expected` (what a
good answer should cover). `user_id` is a fixture persona token such as
usr_anon_t04cab; rows without one use --user.

    python scripts/run_questions.py scripts/eval_questions.csv
    python scripts/run_questions.py questions.csv --api https://<api-host>/ask --limit 10

Writes <input>_results.csv next to the input (or --out). Each request is
signed with JWT_SECRET from .env using the `tester` tier, so a long run
doesn't exhaust the 50/month premium quota.

Built for Groq's free tier (~8,000 tokens/minute, ~200,000 tokens/day per
model; one question is ~7,000): requests go one at a time, --delay defaults
to 60s, a rate-limited question waits a minute and is retried once, and the
run stops after 3 rate-limit failures in a row (the daily allowance is
probably used up). Re-running the same command resumes: questions already
answered in the results file are kept and skipped. Use --fresh to start over.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from pathlib import Path
from typing import Any

import jwt
import requests
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
RATE_LIMIT_WAIT_SECONDS = 65
MAX_CONSECUTIVE_RATE_LIMITS = 3
FIELDS = [
    "user_id", "question", "expected", "status", "question_type", "answer", "period",
    "breakdown", "explanation", "confidence", "cache_hit", "model_used", "latency_ms", "error",
]


def _breakdown_text(table: dict | None) -> str:
    if not table:
        return ""
    lines = [table.get("title") or "", " | ".join(table.get("columns", []))]
    lines += [" | ".join(map(str, row)) for row in table.get("rows", []) + table.get("footer", [])]
    if table.get("note"):
        lines.append(table["note"])
    return "\n".join(line for line in lines if line)


def ask(api: str, secret: str, tier: str, user_id: str, question: str) -> dict:
    token = jwt.encode({"sub": user_id, "tier": tier}, secret, algorithm="HS256")
    started = time.perf_counter()
    try:
        response = requests.post(
            api, json={"user_id": user_id, "question": question},
            headers={"Authorization": f"Bearer {token}"}, timeout=90,
        )
        body = response.json()
    except (requests.RequestException, ValueError) as exc:
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
    row = {"latency_ms": round((time.perf_counter() - started) * 1000)}
    if not response.ok:
        return {**row, "status": response.status_code, "error": f"{body.get('error')}: {body.get('message')}"}
    return {
        **row,
        "status": response.status_code,
        "question_type": body.get("question_type"),
        "answer": body.get("answer"),
        "period": body.get("period"),
        "breakdown": _breakdown_text(body.get("breakdown")),
        "explanation": body.get("explanation"),
        "confidence": body.get("confidence"),
        "cache_hit": body.get("cache_hit"),
        "model_used": body.get("model_used"),
    }


def main() -> int:
    load_dotenv(REPO_ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("questions", type=Path)
    parser.add_argument("--api", default=os.environ.get("API_URL", "http://localhost:8000/ask"))
    parser.add_argument("--user", default=os.environ.get("DEMO_USER_ID", "usr_anon_i07sal"))
    parser.add_argument("--tier", default="tester")
    parser.add_argument("--delay", type=float, default=60.0, help="seconds between requests (default 60)")
    parser.add_argument("--limit", type=int, help="ask at most this many new questions this run")
    parser.add_argument("--fresh", action="store_true", help="ignore earlier results instead of resuming")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    secret = os.environ.get("JWT_SECRET", "dev-only-change-me")
    out = args.out or args.questions.with_name(args.questions.stem + "_results.csv")
    with args.questions.open(encoding="utf-8-sig", newline="") as handle:
        rows = [r for r in csv.DictReader(handle) if (r.get("question") or "").strip()]
    if not rows:
        print("No questions found (expected a 'question' column).", file=sys.stderr)
        return 1

    done = {} if args.fresh else _answered(out)
    pending = [r for r in rows if _key(r, args.user) not in done]
    if args.limit is not None:
        pending = pending[:args.limit]
    print(f"{len(rows)} questions: {len(done)} already answered, asking {len(pending)} now.")

    results = dict(done)
    consecutive_limits = 0
    for index, row in enumerate(pending, 1):
        user_id = (row.get("user_id") or "").strip() or args.user
        question = row["question"].strip()
        result = ask(args.api, secret, args.tier, user_id, question)
        if result.get("status") == 429 and "QUOTA" not in str(result.get("error")):
            print(f"  rate limited; waiting {RATE_LIMIT_WAIT_SECONDS}s and retrying once")
            time.sleep(RATE_LIMIT_WAIT_SECONDS)
            result = ask(args.api, secret, args.tier, user_id, question)
        results[_key(row, args.user)] = {"user_id": user_id, "question": question,
                                         "expected": row.get("expected", ""), **result}
        _write(out, rows, results, args.user)
        print(f"[{index}/{len(pending)}] {user_id} {result.get('status')} {question[:60]}")
        consecutive_limits = consecutive_limits + 1 if result.get("status") == 429 else 0
        if consecutive_limits >= MAX_CONSECUTIVE_RATE_LIMITS:
            print("Stopping: the provider keeps rate-limiting (daily allowance probably used up). "
                  "Run the same command later to resume.")
            break
        if index < len(pending):
            time.sleep(args.delay)

    answered = sum(1 for r in results.values() if str(r.get("status")) == "200")
    print(f"Wrote {out} ({answered}/{len(rows)} answered)")
    return 0


def _key(row: dict[str, Any], default_user: str) -> tuple[str, str]:
    return ((row.get("user_id") or "").strip() or default_user, (row.get("question") or "").strip())


def _answered(out: Path) -> dict[tuple[str, str], dict[str, Any]]:
    """Rows from an earlier run that got a real answer."""
    if not out.exists():
        return {}
    with out.open(encoding="utf-8-sig", newline="") as handle:
        return {(r["user_id"], r["question"]): r for r in csv.DictReader(handle) if r.get("status") == "200"}


def _write(out: Path, rows: list[dict[str, Any]], results: dict, default_user: str) -> None:
    """Rewrite the results file in input order, so it is always complete and
    resumable even if the run is interrupted."""
    with out.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            result = results.get(_key(row, default_user))
            if result:
                writer.writerow(result)


if __name__ == "__main__":
    sys.exit(main())
