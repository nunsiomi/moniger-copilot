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
from functools import lru_cache
from typing import Any

import redis
from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app import cache, prompt_builder, response_parser, router
from app.anonymiser import AnonymisationResult, anonymise
from app.classifier import classify_question
from app.context_builders import build_context
from app.gating import GatingError, authenticate_and_gate

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
    transactions: list[dict[str, Any]] = Field(default_factory=list)
    budgets: list[dict[str, Any]] = Field(default_factory=list)


class AskResponse(BaseModel):
    answer: str
    explanation: str
    confidence: float
    sources: list[str]
    question_type: str
    cache_hit: bool
    model_used: str


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


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


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
    """
    auth = authenticate_and_gate(
        authorization_header=authorization,
        request_user_id=payload.user_id,
        redis_client=redis_client,
        jwt_secret=settings.jwt_secret,
    )

    anonymised = anonymise(payload.question)
    category = classify_question(anonymised.text).category
    context = build_context(category, payload.transactions, payload.budgets)

    fingerprint_hash = cache.fingerprint(category, anonymised.text, context)
    cache_key = cache.build_key(auth.user_id, category, fingerprint_hash)
    cached_entry = cache.read(redis_client, cache_key)

    if cached_entry is not None:
        raw_text = cached_entry["raw_text"]
        model_id = cached_entry["model_used"]
        cache_hit = True
    else:
        system_prompt = prompt_builder.build_system_prompt(category)
        messages = prompt_builder.build_messages(category, anonymised.text, context)
        model_id = router.select_model(category)
        raw_text = router.call_model(model_id, system_prompt, messages)
        cache.write(redis_client, cache_key, {"raw_text": raw_text, "model_used": model_id}, category=category)
        cache_hit = False

    parsed = response_parser.parse(raw_text, question_type=category, cache_hit=cache_hit)
    return _build_response(parsed, model_used=model_id, anonymised=anonymised)


def _build_response(
    parsed: dict[str, Any],
    *,
    model_used: str,
    anonymised: AnonymisationResult,
) -> AskResponse:
    """Restore PII placeholders in `parsed` and shape it into an AskResponse."""
    return AskResponse(
        answer=anonymised.restore(parsed["answer"]),
        explanation=anonymised.restore(parsed["explanation"]),
        confidence=parsed["confidence"],
        sources=[anonymised.restore(source) for source in parsed["sources"]],
        question_type=parsed["question_type"],
        cache_hit=parsed["cache_hit"],
        model_used=model_used,
    )
