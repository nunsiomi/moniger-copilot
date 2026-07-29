"""Quick-question buttons: one-click preset questions used to drive fast
Friday demos (see docs/Moniger_Copilot_Scope_Addendum_Budget.docx, "Friday
Demo Updates") without typing.

This set of six was specified directly rather than mechanically covering
one example per QuestionCategory: spending, budget_track (twice — a
general status check and a category-specific one), tax, budget_recommend,
and comparison. Income, merchant, general, and the out_of_scope_advice
deflection aren't represented here; type a question freehand to exercise
those.
"""
from __future__ import annotations

import streamlit as st

QUICK_QUESTIONS: list[str] = [
    "How much did I spend on food this month?",
    "How am I tracking against my budget?",
    "How does the new tax law affect me?",
    "How should I budget my salary?",
    "How does my spending compare to last month?",
    "How much do I have left in my transport budget?",
]

_COLUMNS_PER_ROW = 3


def render_quick_buttons() -> str | None:
    """Render one button per entry in QUICK_QUESTIONS; return the clicked question, if any."""
    st.markdown('<div class="quick-label">Try asking</div>', unsafe_allow_html=True)

    clicked: str | None = None
    for row_start in range(0, len(QUICK_QUESTIONS), _COLUMNS_PER_ROW):
        row = QUICK_QUESTIONS[row_start : row_start + _COLUMNS_PER_ROW]
        columns = st.columns(len(row))
        for column, question in zip(columns, row):
            with column:
                if st.button(question, key=f"quick::{question}", use_container_width=True):
                    clicked = question
    return clicked
