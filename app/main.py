"""FastAPI app exposing the /ask endpoint.

Request flow: classify -> gate (quota) -> anonymise -> build context ->
cache lookup -> (on miss) route to a model, call it, parse, cache -> restore
PII placeholders -> respond.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any

import redis
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app import cache, gating, prompt_builder, response_parser, router
from app.anonymiser import anonymise
from app.classifier import classify_question
from app.context_builders import build_context


class Settings(BaseSettings):
    redis_url: str = "redis://localhost:6379/0"
    cache_ttl_seconds: int = 86_400
    daily_question_limit: int = 50
    allowed_origins: str = "http://localhost:3000"

    model_config = SettingsConfigDict(env_file=".env")


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


class KeyFigure(BaseModel):
    label: str
    value: str


class AskResponse(BaseModel):
    answer: str
    key_figures: list[KeyFigure]
    follow_up_suggestions: list[str]
    category: str
    model_used: str
    cached: bool


app = FastAPI(title="Moniger Copilot", version="0.1.0")

_settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in _settings.allowed_origins.split(",") if origin.strip()],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/ask", response_model=AskResponse)
def ask(
    payload: AskRequest,
    settings: Settings = Depends(get_settings),
    redis_client: redis.Redis = Depends(get_redis_client),
) -> AskResponse:
    try:
        gating.check_and_increment(
            redis_client,
            payload.user_id,
            daily_limit=settings.daily_question_limit,
        )
    except gating.QuotaExceeded as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc

    category = classify_question(payload.question)
    anonymised = anonymise(payload.question)
    context = build_context(category, payload.transactions)

    cache_key = cache.fingerprint(payload.user_id, category.value, anonymised.text, context)
    cached_response = cache.read(redis_client, cache_key)

    if cached_response is not None:
        return AskResponse(
            answer=anonymised.restore(cached_response["answer"]),
            key_figures=[
                KeyFigure(label=fig["label"], value=anonymised.restore(fig["value"]))
                for fig in cached_response["key_figures"]
            ],
            follow_up_suggestions=cached_response["follow_up_suggestions"],
            category=category.value,
            model_used=cached_response["model_used"],
            cached=True,
        )

    model_id = router.select_model(category)
    system_prompt = prompt_builder.build_system_prompt(category)
    messages = prompt_builder.build_messages(anonymised.text, context)

    raw_text = router.call_model(model_id, system_prompt, messages)
    parsed = response_parser.parse(raw_text)

    cache.write(
        redis_client,
        cache_key,
        {**parsed, "model_used": model_id},
        ttl_seconds=settings.cache_ttl_seconds,
    )

    return AskResponse(
        answer=anonymised.restore(parsed["answer"]),
        key_figures=[
            KeyFigure(label=fig["label"], value=anonymised.restore(fig["value"]))
            for fig in parsed["key_figures"]
        ],
        follow_up_suggestions=parsed["follow_up_suggestions"],
        category=category.value,
        model_used=model_id,
        cached=False,
    )
