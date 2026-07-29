"""Chat interface component: renders conversation history and sends new
questions to the Moniger Copilot /ask endpoint (see app/main.py).
"""
from __future__ import annotations

import html
import os
from typing import Any

import jwt
import requests
import streamlit as st

# Kept as a literal copy of app.prompts.budget_system_prompt.BUDGET_RECOMMEND_DISCLAIMER
# rather than imported from it: `streamlit run demo/app.py` inserts demo/
# at sys.path[0] (Streamlit's own _fix_sys_path), and since that directory
# contains a file literally named app.py, `from app... import ...` anywhere
# under demo/ risks resolving "app" to demo/app.py instead of the real
# top-level app package -- a real, order-dependent import collision hit
# during local testing. If this string ever changes server-side, update it
# here too; the actual guardrail (disclaimer appears exactly once) is
# enforced independently by response_parser._enforce_disclaimer_once
# regardless of what this display-only copy says.
BUDGET_RECOMMEND_DISCLAIMER = "This is general budgeting guidance, not financial advice."

# Demo-only: signed locally with whatever JWT_SECRET the API process itself
# reads (same "dev-only-change-me" default as app.main.Settings), so this
# keeps working even if a developer has customised JWT_SECRET in their own
# .env. This is NOT how a real client authenticates -- production tokens
# come from Moniger's own login flow -- it just lets this local demo call
# /ask without standing one up. "premium" tier avoids tripping the 5/month
# free-tier quota mid-demo.
_DEMO_JWT_SECRET = os.environ.get("JWT_SECRET", "dev-only-change-me")
_DEMO_TIER = "premium"


class DemoAPIError(Exception):
    """Raised when /ask returns a non-200 response, carrying its error envelope."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message


def render_chat(
    api_base_url: str,
    user_id: str,
    transactions: list[dict[str, Any]],
    budgets: list[dict[str, Any]],
    *,
    preset_question: str | None = None,
) -> None:
    """Render the chat history and input box, calling POST /ask on submit.

    Keeps conversation turns in st.session_state, displays the answer plus
    explanation/confidence/sources/cache_hit from the response, and shows a
    friendly error bubble on a non-200 response (e.g. 429 quota exceeded —
    see app/gating.py) instead of crashing the demo. `preset_question` is a
    quick-button click (see quick_buttons.py) — Streamlit's chat_input can't
    be pre-filled programmatically, so a quick button submits directly
    rather than populating the text box first.
    """
    st.session_state.setdefault("messages", [])

    typed_question = st.chat_input("Ask about your spending, income, tax, or budgets…")
    question = preset_question or typed_question

    if question:
        _submit_question(api_base_url, user_id, question, transactions, budgets)

    _render_history()


def _submit_question(
    api_base_url: str,
    user_id: str,
    question: str,
    transactions: list[dict[str, Any]],
    budgets: list[dict[str, Any]],
) -> None:
    st.session_state.messages.append({"role": "user", "content": question})
    try:
        with st.spinner("Reading your transaction history…"):
            result = _call_ask_endpoint(api_base_url, user_id, question, transactions, budgets)
        st.session_state.messages.append({"role": "assistant", "ok": True, **result})
    except DemoAPIError as exc:
        st.session_state.messages.append(
            {"role": "assistant", "ok": False, "error_code": exc.error_code, "message": exc.message}
        )
    except requests.RequestException:
        st.session_state.messages.append(
            {
                "role": "assistant",
                "ok": False,
                "error_code": "ERR_CONNECTION",
                "message": (
                    f"Couldn't reach the API at {api_base_url}. Is it running? "
                    "(`uvicorn app.main:app --reload`)"
                ),
            }
        )


def _call_ask_endpoint(
    api_base_url: str,
    user_id: str,
    question: str,
    transactions: list[dict[str, Any]],
    budgets: list[dict[str, Any]],
) -> dict[str, Any]:
    """POST `question` (plus supporting data) to /ask and return the parsed JSON response."""
    token = jwt.encode({"sub": user_id, "tier": _DEMO_TIER}, _DEMO_JWT_SECRET, algorithm="HS256")
    response = requests.post(
        f"{api_base_url}/ask",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "user_id": user_id,
            "question": question,
            "transactions": transactions,
            "budgets": budgets,
        },
        timeout=30,
    )
    payload = response.json()
    if response.status_code != 200:
        raise DemoAPIError(payload.get("error", "ERR_UNKNOWN"), payload.get("message", "Something went wrong."))
    return payload


def _render_history() -> None:
    for message in st.session_state.messages:
        if message["role"] == "user":
            _render_user_turn(message["content"])
        elif message.get("ok") is False:
            _render_error_turn(message)
        else:
            _render_assistant_turn(message)


def _render_user_turn(content: str) -> None:
    st.markdown(
        f"""
        <div class="turn turn-user">
          <div class="bubble bubble-user">{html.escape(content)}</div>
          <div class="avatar avatar-user">U</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_error_turn(message: dict[str, Any]) -> None:
    st.markdown(
        f"""
        <div class="turn turn-assistant">
          <div class="avatar avatar-assistant">₦</div>
          <div class="bubble bubble-error">
            <div class="chip-row"><span class="chip chip-error">{html.escape(str(message.get('error_code', 'ERR_UNKNOWN')))}</span></div>
            <div class="answer">{html.escape(str(message.get('message', 'Something went wrong.')))}</div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_assistant_turn(message: dict[str, Any]) -> None:
    answer, explanation, disclaimer = _split_out_disclaimer(
        str(message.get("answer", "")), str(message.get("explanation", ""))
    )
    question_type = str(message.get("question_type", "general"))
    confidence = _clamp01(message.get("confidence", 0.0))
    cache_hit = bool(message.get("cache_hit"))
    sources = message.get("sources") or []

    chips = f'<span class="chip chip-type">{html.escape(question_type.replace("_", " "))}</span>'
    if cache_hit:
        chips += '<span class="chip chip-cache">⚡ from cache</span>'

    sources_html = ""
    if sources:
        joined = ", ".join(html.escape(str(source)) for source in sources)
        sources_html = f'<div class="sources">Sources: {joined}</div>'

    disclaimer_html = f'<div class="disclaimer">{html.escape(disclaimer)}</div>' if disclaimer else ""

    st.markdown(
        f"""
        <div class="turn turn-assistant">
          <div class="avatar avatar-assistant">₦</div>
          <div class="bubble bubble-assistant">
            <div class="chip-row">{chips}</div>
            <div class="answer">{html.escape(answer)}</div>
            <div class="explanation">{html.escape(explanation)}</div>
            {_confidence_bar_html(confidence)}
            {sources_html}
            {disclaimer_html}
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _split_out_disclaimer(answer: str, explanation: str) -> tuple[str, str, str | None]:
    """Pull BUDGET_RECOMMEND_DISCLAIMER out of wherever the model placed it
    (answer or explanation — response_parser guarantees it appears exactly
    once across the two, not which field), so it can be re-rendered
    separately in smaller, muted text instead of blending into the main
    answer/explanation copy."""
    disclaimer: str | None = None
    if BUDGET_RECOMMEND_DISCLAIMER in answer:
        answer = answer.replace(BUDGET_RECOMMEND_DISCLAIMER, "").strip()
        disclaimer = BUDGET_RECOMMEND_DISCLAIMER
    if BUDGET_RECOMMEND_DISCLAIMER in explanation:
        explanation = explanation.replace(BUDGET_RECOMMEND_DISCLAIMER, "").strip()
        disclaimer = BUDGET_RECOMMEND_DISCLAIMER
    return answer, explanation, disclaimer


def _clamp01(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _confidence_bar_html(confidence: float) -> str:
    """Return a single-line HTML fragment -- deliberately no embedded
    newlines. This gets interpolated inline into _render_assistant_turn's
    own multi-line markup; a multi-line fragment here would leave a
    whitespace-only "blank" line at the splice point, which Streamlit's
    underlying Markdown parser treats as the end of the raw-HTML block,
    silently downgrading everything after it (the sources/disclaimer divs)
    into a literal, unrendered code block instead of parsed HTML.
    """
    percent = round(confidence * 100)
    return (
        '<div class="confidence-row">'
        '<span class="confidence-label">Confidence</span>'
        f'<div class="confidence-track"><div class="confidence-fill" style="width:{percent}%"></div></div>'
        f'<span class="confidence-value">{percent}%</span>'
        "</div>"
    )
