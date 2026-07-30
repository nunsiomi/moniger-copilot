"""Streamlit demo app for Moniger Copilot.

Thin UI shell used for Friday demos (see
docs/Moniger_Copilot_Scope_Addendum_Budget.docx, "Friday Demo Updates") —
renders a chat interface backed by the /ask endpoint (app/main.py), loaded
by default with the synthetic transactions and budgets under data/. A
sidebar lets a user upload their own transactions/budgets (JSON or CSV) to
replace that default for the session — see _render_data_sidebar. All
actual chat rendering and API-calling logic lives in demo/components/;
this module wires the page together, owns the visual theme, and owns the
upload/session-data handling.

Run with: streamlit run demo/app.py
"""
from __future__ import annotations

# Must run before the components.chat import below: it reads JWT_SECRET
# directly with os.environ.get(...) at module load time, and nothing else
# in this process loads .env into the environment otherwise.
from dotenv import load_dotenv

load_dotenv()

import csv
import io
import json
import os
from pathlib import Path
from typing import Any

import streamlit as st

# Imported relative to this file's own directory (not "demo.components...")
# because Streamlit's script runner only ever puts this script's own
# directory on sys.path, not the repo root -- true both locally and on
# Streamlit Community Cloud. Locally, `python -m streamlit run demo/app.py`
# happens to also add the repo root (via Python's own -m behaviour), which
# is why a "demo."-prefixed import used to work on a dev machine but fails
# on Cloud's runner, which doesn't do that.
from components.chat import render_chat
from components.quick_buttons import render_quick_buttons

# Defaults to localhost for local dev; set API_BASE_URL in Streamlit Cloud's
# secrets (or any other env) to point at the deployed FastAPI backend instead
# — Streamlit Community Cloud only hosts this UI, not app/main.py or Redis.
API_BASE_URL = os.environ.get("API_BASE_URL", "http://localhost:8000")
DEFAULT_USER_ID = "user_001"
TRANSACTIONS_PATH = Path(__file__).resolve().parent.parent / "data" / "synthetic_transactions.json"
BUDGETS_PATH = Path(__file__).resolve().parent.parent / "data" / "synthetic_budgets.json"

# A dark, warm "ledger" theme — deep charcoal-green canvas, naira-gold and
# emerald accents, a serif display face for the wordmark against a
# geometric sans for everything else and a mono face for figures/badges —
# instead of Streamlit's default light theme.
_PAGE_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght@0,9..144,500;0,9..144,600;1,9..144,500&family=Sora:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap');

:root {
  --bg: #0c1310;
  --surface: #141f1a;
  --surface-raised: #182620;
  --border: #26372d;
  --border-soft: rgba(217, 169, 79, 0.28);
  --gold: #d9a94f;
  --gold-soft: #e8c97d;
  --emerald: #2f9e67;
  --emerald-deep: #1c6b46;
  --paper: #f3efe4;
  --muted: #96a89c;
  --danger: #e2665a;
}

html, body, [class*="css"] { font-family: 'Sora', sans-serif; }

.stApp {
  background-color: var(--bg);
  background-image:
    radial-gradient(ellipse 900px 480px at 50% -8%, rgba(217, 169, 79, 0.14), transparent 60%),
    radial-gradient(ellipse 700px 480px at 105% 105%, rgba(47, 158, 103, 0.14), transparent 60%);
  color: var(--paper);
}

#MainMenu, footer, header[data-testid="stHeader"] { visibility: hidden; height: 0; }

.block-container { padding-top: 2.25rem; max-width: 700px; }

/* Header */
.app-header { text-align: center; margin-bottom: 1.5rem; }
.app-kicker {
  font-family: 'JetBrains Mono', monospace;
  font-size: 0.72rem;
  letter-spacing: 0.4em;
  color: var(--gold);
}
.app-title {
  font-family: 'Fraunces', serif;
  font-weight: 600;
  font-size: 3.1rem;
  color: var(--paper);
  margin: 0.1rem 0 0 0;
  line-height: 1;
}
.app-subtitle {
  font-size: 0.92rem;
  color: var(--muted);
  margin-top: 0.65rem;
}

/* Quick-question buttons */
.quick-label {
  font-family: 'JetBrains Mono', monospace;
  font-size: 0.68rem;
  letter-spacing: 0.22em;
  text-transform: uppercase;
  color: var(--muted);
  margin: 0.4rem 0 0.7rem 0;
}
div[data-testid="stButton"] > button {
  background: transparent;
  border: 1px solid var(--border-soft);
  color: var(--gold-soft);
  border-radius: 999px;
  font-family: 'Sora', sans-serif;
  font-size: 0.8rem;
  padding: 0.55rem 0.9rem;
  transition: all 0.18s ease;
  white-space: normal;
  line-height: 1.25;
}
div[data-testid="stButton"] > button:hover {
  background: var(--emerald);
  border-color: var(--emerald);
  color: var(--paper);
  transform: translateY(-1px);
  box-shadow: 0 8px 18px rgba(0, 0, 0, 0.35);
}
div[data-testid="stButton"] > button:active { transform: translateY(0); }

/* Chat turns */
.turn { display: flex; align-items: flex-end; gap: 0.5rem; margin: 0.9rem 0; }
.turn-user { justify-content: flex-end; }
.turn-assistant { justify-content: flex-start; }

.avatar {
  flex-shrink: 0;
  width: 30px;
  height: 30px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-family: 'JetBrains Mono', monospace;
  font-size: 0.8rem;
  font-weight: 600;
}
.avatar-user { background: transparent; border: 1px solid var(--emerald); color: var(--emerald); }
.avatar-assistant { background: var(--gold); color: #221604; }

.bubble { max-width: 76%; padding: 0.8rem 1.05rem; font-size: 0.93rem; line-height: 1.55; }
.bubble-user {
  background: linear-gradient(135deg, var(--emerald), var(--emerald-deep));
  color: var(--paper);
  border-radius: 16px 16px 4px 16px;
  box-shadow: 0 4px 14px rgba(0, 0, 0, 0.3);
}
.bubble-assistant {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 16px 16px 16px 4px;
}
.bubble-error {
  background: rgba(226, 102, 90, 0.1);
  border: 1px solid rgba(226, 102, 90, 0.4);
  border-radius: 16px 16px 16px 4px;
}

.chip-row { display: flex; gap: 0.4rem; margin-bottom: 0.55rem; flex-wrap: wrap; }
.chip {
  font-family: 'JetBrains Mono', monospace;
  font-size: 0.64rem;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  padding: 0.2rem 0.55rem;
  border-radius: 999px;
}
.chip-type { border: 1px solid var(--border-soft); color: var(--gold-soft); }
.chip-cache { background: rgba(47, 158, 103, 0.2); color: #86e0ae; }
.chip-error { background: rgba(226, 102, 90, 0.2); color: #f3a89f; }

.answer { color: var(--paper); }
.explanation { color: var(--muted); font-size: 0.83rem; margin-top: 0.5rem; }

.confidence-row { display: flex; align-items: center; gap: 0.55rem; margin: 0.6rem 0 0.2rem 0; }
.confidence-label {
  font-family: 'JetBrains Mono', monospace;
  font-size: 0.66rem;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--muted);
  white-space: nowrap;
}
.confidence-track { flex: 1; height: 5px; border-radius: 4px; background: var(--border); overflow: hidden; }
.confidence-fill { height: 100%; border-radius: 4px; background: linear-gradient(90deg, var(--gold), var(--emerald)); }
.confidence-value { font-family: 'JetBrains Mono', monospace; font-size: 0.72rem; color: var(--paper); }

.sources { font-size: 0.78rem; color: var(--muted); margin-top: 0.5rem; }

.disclaimer {
  font-size: 0.72rem;
  font-style: italic;
  color: var(--muted);
  border-top: 1px solid var(--border);
  padding-top: 0.5rem;
  margin-top: 0.65rem;
}

/* Chat input */
[data-testid="stChatInput"] { background: transparent; }
[data-testid="stChatInput"] textarea {
  background: var(--surface-raised) !important;
  border: 1px solid var(--border-soft) !important;
  color: var(--paper) !important;
  font-family: 'Sora', sans-serif !important;
}
[data-testid="stBottomBlockContainer"] { background: var(--bg); }

/* Sidebar (data upload) */
[data-testid="stSidebar"] {
  background: var(--surface);
  border-right: 1px solid var(--border);
}
.sidebar-title {
  font-family: 'JetBrains Mono', monospace;
  font-size: 0.72rem;
  letter-spacing: 0.3em;
  text-transform: uppercase;
  color: var(--gold);
  margin-bottom: 0.4rem;
}
[data-testid="stSidebar"] [data-testid="stFileUploaderDropzone"] {
  background: var(--surface-raised);
  border: 1px dashed var(--border-soft);
}
.data-status {
  font-family: 'JetBrains Mono', monospace;
  font-size: 0.72rem;
  color: var(--muted);
  margin: 0.6rem 0;
}
</style>
"""


def _load_json(path: Path) -> dict:
    """Load and return the JSON fixture at `path`."""
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _parse_upload(uploaded_file: Any, *, wrapper_key: str) -> list[dict[str, Any]]:
    """Parse an uploaded JSON or CSV file into a list of record dicts.

    JSON may be either the fixture shape ({"transactions": [...]} /
    {"budgets": [...]}) or a bare list. `amount` (the one field every
    downstream context_builder does arithmetic on) is coerced to a float
    where possible in both formats -- CSV rows come back as strings from
    csv.DictReader by construction, and JSON amounts occasionally arrive
    as strings too depending on how a user's own export serialized them.
    Every other field (dates, categories, ids) is left as-is, since
    context_builders already reads those defensively via `.get()`.
    """
    name = uploaded_file.name.lower()
    raw_bytes = uploaded_file.getvalue()

    if name.endswith(".json"):
        try:
            data = json.loads(raw_bytes.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON: {exc}") from exc
        if isinstance(data, dict) and wrapper_key in data:
            data = data[wrapper_key]
        if not isinstance(data, list):
            raise ValueError(f"Expected a JSON list, or an object with a {wrapper_key!r} key containing one.")
        return _coerce_amounts(data)

    if name.endswith(".csv"):
        text = raw_bytes.decode("utf-8-sig")
        rows = [dict(row) for row in csv.DictReader(io.StringIO(text))]
        return _coerce_amounts(rows)

    raise ValueError("Unsupported file type — please upload a .json or .csv file.")


def _coerce_amounts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Coerce each row's `amount` field to a float where possible, in place."""
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("amount"), str) and row["amount"]:
            try:
                row["amount"] = float(row["amount"])
            except ValueError:
                pass
    return rows


def _render_data_sidebar(
    default_transactions: list[dict[str, Any]],
    default_budgets: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Render upload controls for replacing the synthetic data this session.

    Uploaded data lives only in st.session_state -- it never touches the
    files under data/, and reverts to the synthetic fixtures on a fresh
    session or after "Reset to synthetic demo data".
    """
    st.sidebar.markdown('<div class="sidebar-title">Your data</div>', unsafe_allow_html=True)
    st.sidebar.caption("Upload your own transactions/budgets (JSON or CSV) to replace the synthetic demo data for this session.")

    txn_file = st.sidebar.file_uploader("Transactions", type=["json", "csv"], key="txn_upload")
    if txn_file is not None:
        upload_key = (txn_file.name, txn_file.size)
        if st.session_state.get("_txn_upload_key") != upload_key:
            try:
                st.session_state["transactions"] = _parse_upload(txn_file, wrapper_key="transactions")
                st.session_state["_txn_upload_key"] = upload_key
                st.sidebar.success(f"Loaded {len(st.session_state['transactions'])} transactions for this session.")
            except ValueError as exc:
                st.sidebar.error(f"Couldn't parse transactions file: {exc}")

    budget_file = st.sidebar.file_uploader("Budgets", type=["json", "csv"], key="budget_upload")
    if budget_file is not None:
        upload_key = (budget_file.name, budget_file.size)
        if st.session_state.get("_budget_upload_key") != upload_key:
            try:
                st.session_state["budgets"] = _parse_upload(budget_file, wrapper_key="budgets")
                st.session_state["_budget_upload_key"] = upload_key
                st.sidebar.success(f"Loaded {len(st.session_state['budgets'])} budgets for this session.")
            except ValueError as exc:
                st.sidebar.error(f"Couldn't parse budgets file: {exc}")

    transactions = st.session_state.get("transactions", default_transactions)
    budgets = st.session_state.get("budgets", default_budgets)

    st.sidebar.markdown(
        f'<div class="data-status">{len(transactions)} transactions &middot; {len(budgets)} budgets loaded</div>',
        unsafe_allow_html=True,
    )
    if st.sidebar.button("Reset to synthetic demo data", use_container_width=True):
        for key in ("transactions", "budgets", "_txn_upload_key", "_budget_upload_key"):
            st.session_state.pop(key, None)
        st.rerun()

    return transactions, budgets


def _render_header() -> None:
    st.markdown(
        """
        <div class="app-header">
          <div class="app-kicker">MONIGER</div>
          <h1 class="app-title">Copilot</h1>
          <p class="app-subtitle">Ask about your money — answers grounded in your real transactions, not guesses.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )


def main() -> None:
    """Configure the page, load synthetic fixtures, and render the demo UI."""
    st.set_page_config(page_title="Moniger Copilot", page_icon="₦", layout="centered")
    st.markdown(_PAGE_CSS, unsafe_allow_html=True)

    default_transactions = _load_json(TRANSACTIONS_PATH).get("transactions", [])
    default_budgets = _load_json(BUDGETS_PATH).get("budgets", [])
    transactions, budgets = _render_data_sidebar(default_transactions, default_budgets)

    _render_header()
    quick_question = render_quick_buttons()
    render_chat(API_BASE_URL, DEFAULT_USER_ID, transactions, budgets, preset_question=quick_question)


if __name__ == "__main__":
    main()
