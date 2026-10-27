"""FastAPI app exposing the /ask endpoint.

Request flow: authenticate + gate (JWT + monthly tiered quota, app.gating)
-> anonymise -> classify -> build context -> cache lookup -> (on miss)
route to a model, call it, cache its raw text -> parse (always, hit or
miss alike) -> restore PII placeholders -> respond. See
docs/Moniger_Copilot_Engineering.docx for the original design; JWT auth,
monthly tiered quotas, per-category cache TTLs, and a Haiku-backed (not
keyword-based) classifier supersede that doc's simpler schemes (see
CLAUDE.md).

The cache stores the model's raw text + which model produced it, not a
pre-parsed result — response_parser.parse runs on every request either
way, so `cache_hit` in its output always reflects the current request
rather than being frozen at whatever value it had when the entry was
first written.

Anonymise runs BEFORE classify, not after: classify_question makes its own
Groq model call (see app/classifier.py), and no raw PII may reach any
model call, classification included — so the question must already be
anonymised by the time it gets there. Cache lookup then happens after
classify/build_context, since the cache key (app.cache.build_key) is keyed
in part on the classified category and the anonymised-question+context
hash — neither of which exist until those steps have run.

Every error response is uniform JSON: {"error": "ERR_CODE", "message":
"..."}. ERR_JWT_INVALID / ERR_USER_MISMATCH / ERR_QUOTA_EXCEEDED come from
app.gating; ERR_VALIDATION / ERR_INTERNAL are added here for the same
uniform contract on request-validation and unexpected-error responses
respectively. None of this envelope is defined in
docs/Moniger_Copilot_Contract.docx (it's a generic SOW template with no
technical schema) — it was agreed directly with the project owner.
"""
from __future__ import annotations

# Must run before the `app.*` imports below: router.py/classifier.py read
# GROQ_API_KEY (via the Groq SDK) and their model-ID env vars directly with
# os.environ.get(...), not through the Settings class further down (whose
# own env_file=".env" loading is scoped to its own declared fields only —
# it never populates the process environment for anything else to see).
from dotenv import load_dotenv

load_dotenv()

import logging
import hashlib
import os
from functools import lru_cache
from typing import Any

import redis
from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict
from groq import APIError

from app import breakdown, cache, data_access, prompt_builder, response_parser, router
from app.anonymiser import AnonymisationResult, anonymise
from app.classifier import QuestionCategory, classify_question, ClassifierUnavailableError
from app.context_builders import build_context
from app.gating import GatingError, authenticate_and_gate, extract_claims, UserMismatchError
from app.mirror_validation import MirrorValidationError

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    """The narrow slice of config app/main.py itself needs. Other modules
    (router.py, classifier.py, gating.py, cache.py) read their own env vars
    directly via os.environ.get(...) rather than through this class, so
    extra="ignore" is required here — .env carries plenty of vars (GROQ_*,
    APP_ENV, AWS_REGION, etc.) this model was never meant to declare, and
    pydantic-settings' default of rejecting undeclared vars would otherwise
    fail Settings() at import time the moment a real .env file exists.
    """

    redis_url: str = "redis://localhost:6379/0"
    jwt_secret: str = "dev-only-change-me"
    allowed_origins: str = "http://localhost:3000"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


@lru_cache(maxsize=1)
def get_redis_client() -> redis.Redis:
    return redis.Redis.from_url(get_settings().redis_url, decode_responses=True)


class AskRequest(BaseModel):
    user_id: str
    question: str


class AskResponse(BaseModel):
    answer: str
    explanation: str
    period: str | None = None
    confidence: float
    sources: list[str]
    question_type: str
    cache_hit: bool
    model_used: str
    breakdown: dict[str, Any] | None = None


app = FastAPI(title="Moniger Copilot", version="0.1.0")

_settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in _settings.allowed_origins.split(",") if origin.strip()],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(GatingError)
def handle_gating_error(request: Request, exc: GatingError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"error": exc.error_code, "message": exc.message})


@app.exception_handler(RequestValidationError)
def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"error": "ERR_VALIDATION", "message": "Request body failed validation."},
    )


@app.exception_handler(Exception)
def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error in %s", request.url.path)
    return JSONResponse(
        status_code=500,
        content={"error": "ERR_INTERNAL", "message": "An unexpected error occurred."},
    )


@app.exception_handler(MirrorValidationError)
def handle_mirror_error(request: Request, exc: MirrorValidationError) -> JSONResponse:
    return JSONResponse(status_code=503, content={
        "error": "ERR_MIRROR_VALIDATION",
        "message": "Financial data failed validation. Please try again after the data source is corrected.",
    })


@app.exception_handler(APIError)
@app.exception_handler(ClassifierUnavailableError)
def handle_provider_error(request: Request, exc: Exception) -> JSONResponse:
    cause = exc.__cause__ if isinstance(exc, ClassifierUnavailableError) and exc.__cause__ else exc
    provider_status = getattr(cause, "status_code", None)
    # Never log the exception body: it can contain prompts or generated text.
    logger.warning("AI request failed: stage=%s type=%s provider_status=%s",
                   "classification" if isinstance(exc, ClassifierUnavailableError) else "answer",
                   type(cause).__name__, provider_status)
    error, message, status = "ERR_MODEL_UNAVAILABLE", "The AI provider is temporarily unavailable. Please try again later.", 503
    if provider_status == 429:
        error, message, status = "ERR_MODEL_RATE_LIMIT", "The AI provider's request or token limit was reached. Pause before trying again; repeated retries will not help.", 429
    elif provider_status == 403:
        message = "The AI provider denied this connection. Check that your VPN is disconnected and your provider access is enabled."
    elif provider_status == 401:
        message = "The AI provider rejected the configured API key. Check the server's API key configuration."
    elif provider_status in (400, 413, 422):
        error = "ERR_MODEL_REQUEST"
        message = "The AI provider could not process this request or produce the required answer format. Try a shorter, single-topic question."
    elif type(cause).__name__ == "APITimeoutError":
        error = "ERR_MODEL_TIMEOUT"
        message = "The AI provider took too long to respond. Please try again shortly."
    return JSONResponse(status_code=status, content={"error": error, "message": message})


@app.on_event("startup")
def _create_mongo_indexes() -> None:
    """Idempotent — see app/data_access.py's ensure_indexes. Logged rather
    than fatal on failure so a slow/unreachable Mongo at boot never takes
    the whole app down; /ask itself will surface a real error per-request
    if Mongo is genuinely unavailable.
    """
    if os.environ.get("MONGODB_CREATE_INDEXES", "false").lower() != "true":
        return
    try:
        data_access.ensure_indexes()
    except Exception:
        logger.exception("Failed to ensure MongoDB indexes at startup.")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/demo/summary")
def demo_summary(user_id: str, authorization: str | None = Header(default=None),
                 settings: Settings = Depends(get_settings)):
    """Explicit development-only data preview. No model calls or quota charges."""
    if os.environ.get("APP_ENV") != "development":
        return JSONResponse(status_code=404, content={"error": "ERR_NOT_FOUND", "message": "Not found."})
    auth = extract_claims(authorization, secret=settings.jwt_secret)
    if auth.user_id != user_id:
        raise UserMismatchError("Authenticated user does not match requested user.")
    transactions = data_access.get_transactions(auth.user_id)
    budgets = data_access.get_budgets(auth.user_id)
    as_of = data_access.get_as_of_date()
    return {
        "mode": "data_preview_no_ai", "as_of_date": as_of.isoformat(),
        "spending": build_context(QuestionCategory.SPENDING, transactions, budgets, as_of),
        "income": build_context(QuestionCategory.INCOME, transactions, budgets, as_of),
        "budget": build_context(QuestionCategory.BUDGET_TRACK, transactions, budgets, as_of),
        "allocation": build_context(QuestionCategory.BUDGET_RECOMMEND, transactions, budgets, as_of),
    }


@app.post("/ask", response_model=AskResponse)
def ask(
    payload: AskRequest,
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
    redis_client: redis.Redis = Depends(get_redis_client),
) -> AskResponse:
    """Authenticate + gate, anonymise, classify, build context, check the
    cache, and on a miss call the model, parse, cache, then respond with
    PII placeholders restored. See the module docstring for the full flow.

    Transactions/budgets are never accepted from the request body — they
    are looked up server-side from the Mongo mirror by `payload.user_id`
    (see app/data_access.py). The pipeline that populates the mirror is
    backend-owned; Copilot only ever queries it.
    """
    auth = authenticate_and_gate(
        authorization_header=authorization,
        request_user_id=payload.user_id,
        redis_client=redis_client,
        jwt_secret=settings.jwt_secret,
    )

    transactions = data_access.get_transactions(auth.user_id)
    # Validate the mirror before any model call, including classification.
    budgets = data_access.get_budgets(auth.user_id)
    anonymised = anonymise(payload.question)
    category_key = "copilot:intent:v2:" + hashlib.sha256(
        (auth.user_id + ":" + anonymised.text.strip().lower()).encode()
    ).hexdigest()
    cached_category = cache.read(redis_client, category_key)
    if cached_category is not None:
        category = QuestionCategory(cached_category["category"])
    else:
        classification = classify_question(anonymised.text)
        category = classification.category
        if classification.confidence > 0:
            cache.write(redis_client, category_key, {"category": category.value}, category=category)
    needs_budgets = category in (QuestionCategory.BUDGET_TRACK, QuestionCategory.BUDGET_RECOMMEND)
    as_of = data_access.get_as_of_date() if needs_budgets else None
    context = build_context(category, transactions, budgets, as_of, question=anonymised.text)

    # The prompt is built (no network) before the cache lookup so the key
    # covers exactly what the model would see: a prompt, trimming or model
    # change then invalidates old answers by itself.
    system_prompt = prompt_builder.build_system_prompt(category)
    messages = prompt_builder.build_messages(category, anonymised.text, context)
    model_id = router.select_model(category)
    fingerprint_hash = cache.fingerprint(
        category, anonymised.text, context, prompt=f"{model_id}\n{system_prompt}\n{messages}"
    )
    cache_key = cache.build_key(auth.user_id, category, fingerprint_hash)
    cached_entry = cache.read(redis_client, cache_key)

    if cached_entry is not None:
        raw_text = cached_entry["raw_text"]
        model_id = cached_entry["model_used"]
        cache_hit = True
    else:
        raw_text = router.call_model(model_id, system_prompt, messages)
        cache_hit = False

    parsed = response_parser.parse(raw_text, question_type=category, cache_hit=cache_hit)
    if not cache_hit:
        cache.write(redis_client, cache_key, {"raw_text": raw_text, "model_used": model_id}, category=category)
    return _build_response(parsed, model_used=model_id, anonymised=anonymised,
                           table=breakdown.build_breakdown(category, context))


def _build_response(
    parsed: dict[str, Any],
    *,
    model_used: str,
    anonymised: AnonymisationResult,
    table: dict[str, Any] | None = None,
) -> AskResponse:
    """Restore PII placeholders in `parsed` and shape it into an AskResponse."""
    return AskResponse(
        answer=anonymised.restore(parsed["answer"]),
        explanation=anonymised.restore(parsed["explanation"]),
        period=parsed.get("period"),
        confidence=parsed["confidence"],
        sources=[anonymised.restore(source) for source in parsed["sources"]],
        question_type=parsed["question_type"],
        cache_hit=parsed["cache_hit"],
        model_used=model_used,
        breakdown=table,
    )
