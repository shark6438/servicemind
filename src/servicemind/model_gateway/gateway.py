from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from functools import cached_property
from typing import Any
from uuid import UUID

import tiktoken
from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel

from schema.models import DeepseekModelName
from servicemind.model_gateway.cache import SemanticModelCache
from servicemind.model_gateway.contracts import (
    ModelCallContext,
    ModelInvocationAudit,
    ModelPurpose,
    ModelRisk,
    ModelRouteDecision,
    stable_hash,
)
from servicemind.model_gateway.policy import ModelRoutePolicy, SettingsModelRoutePolicy
from servicemind.model_gateway.repository import ConfiguredModelAuditSink, ModelAuditSink

_active_context: ContextVar[ModelCallContext | None] = ContextVar(
    "servicemind_model_call_context", default=None
)


@contextmanager
def model_call_scope(context: ModelCallContext) -> Iterator[None]:
    token = _active_context.set(context)
    try:
        yield
    finally:
        _active_context.reset(token)


def _model_identity(model: BaseChatModel) -> tuple[str, str, str]:
    name = str(getattr(model, "model_name", getattr(model, "model", "unknown")))
    module = type(model).__module__.casefold()
    provider = "deepseek" if name in {item.value for item in DeepseekModelName} else "unknown"
    for marker, value in (
        ("azure", "azure"),
        ("openai", "openai"),
        ("anthropic", "anthropic"),
        ("bedrock", "aws"),
        ("google", "google"),
        ("vertex", "vertexai"),
        ("groq", "groq"),
        ("ollama", "ollama"),
        ("fake", "fake"),
    ):
        if marker in module and provider == "unknown":
            provider = value
    revision = str(
        getattr(model, "model_version", None) or getattr(model, "model_revision", None) or name
    )
    return provider, name, revision


def _structured[SchemaT: BaseModel](model: BaseChatModel, schema: type[SchemaT]):
    name = str(getattr(model, "model_name", getattr(model, "model", "")))
    if name in {item.value for item in DeepseekModelName}:
        return model.with_structured_output(schema, method="json_mode", include_raw=True)
    return model.with_structured_output(schema, include_raw=True)


def _message_payload(messages: Any) -> Any:
    if isinstance(messages, Sequence) and not isinstance(messages, (str, bytes)):
        return [
            {
                "type": getattr(item, "type", type(item).__name__),
                "content": getattr(item, "content", str(item)),
            }
            for item in messages
        ]
    return messages


def _error_code(error: BaseException) -> str:
    name = type(error).__name__.upper()
    if isinstance(error, TimeoutError):
        return "MODEL_TIMEOUT"
    if "RATE" in name or "429" in str(error):
        return "MODEL_RATE_LIMITED"
    if "VALIDATION" in name or "JSON" in name:
        return "MODEL_SCHEMA_INVALID"
    return f"MODEL_{name[:80]}"


def _retryable(error: BaseException) -> bool:
    name = type(error).__name__.casefold()
    message = str(error).casefold()
    return isinstance(error, TimeoutError) or any(
        marker in name or marker in message
        for marker in (
            "timeout",
            "ratelimit",
            "rate_limit",
            "validation",
            "json",
            "429",
            "500",
            "502",
            "503",
            "504",
        )
    )


class ModelCostBudgetExceeded(RuntimeError):
    pass


class ModelGateway:
    """Single governed path for provider validation, schema output, retry and audit."""

    def __init__(
        self,
        *,
        policy: ModelRoutePolicy | None = None,
        audit_sink: ModelAuditSink | None = None,
        cache: SemanticModelCache | None = None,
        max_retries: int | None = None,
        prices_per_million: dict[str, tuple[float, float]] | None = None,
    ) -> None:
        self.policy = policy or SettingsModelRoutePolicy()
        self.audit_sink = audit_sink or ConfiguredModelAuditSink()
        self.cache = cache or SemanticModelCache()
        self.max_retries = max_retries
        # Conservative peak/cache-miss rates from DeepSeek's pricing effective
        # 2026-08-16. Provider-reported token counts are preferred; cost remains an
        # estimate because cache billing detail is not guaranteed in every adapter.
        self.prices = prices_per_million or {
            "deepseek-v4-flash": (0.44, 1.32),
            "deepseek-v4-pro": (1.32, 3.96),
        }
        self._failures: dict[str, int] = {}
        self._open_until: dict[str, float] = {}

    @cached_property
    def _encoding(self) -> tiktoken.Encoding:
        # tiktoken downloads the cl100k_base BPE file on first use and caches
        # it; keep that out of __init__ so constructing the gateway (and thus
        # importing this module) never blocks on the network.
        return tiktoken.get_encoding("cl100k_base")

    def structured[SchemaT: BaseModel](
        self,
        model: BaseChatModel,
        schema: type[SchemaT],
        *,
        context: ModelCallContext | None = None,
        fallback_models: Sequence[BaseChatModel] = (),
    ) -> GovernedStructuredRunnable[SchemaT]:
        return GovernedStructuredRunnable(self, model, schema, context, fallback_models)

    async def invoke[SchemaT: BaseModel](
        self,
        model: BaseChatModel,
        schema: type[SchemaT],
        messages: Any,
        *,
        context: ModelCallContext,
        fallback_models: Sequence[BaseChatModel] = (),
        config: Any = None,
        **kwargs: Any,
    ) -> SchemaT:
        payload = _message_payload(messages)
        prompt_hash = stable_hash(payload)
        schema_hash = stable_hash(schema.model_json_schema())
        input_tokens = len(self._encoding.encode(str(payload)))
        models = [model]
        if self.policy.allow_fallback(context):
            models.extend(fallback_models)
        primary_identity = _model_identity(model)
        last_error: BaseException | None = None
        total_attempts = 0

        max_retries = self.max_retries
        if max_retries is None:
            from core import settings

            max_retries = settings.SERVICEMIND_MODEL_MAX_RETRIES
        for candidate_index, candidate in enumerate(models):
            provider, name, revision = _model_identity(candidate)
            route = self.policy.decide(
                provider=provider,
                model=name,
                revision=revision,
                context=context,
            )
            if self._open_until.get(provider, 0) > time.monotonic():
                last_error = RuntimeError(f"model circuit is open: {provider}")
                continue
            cache_key = stable_hash(
                {
                    "tenant": str(context.tenant_id),
                    "agent": context.agent_role,
                    "purpose": context.purpose.value,
                    "policy": context.policy_version,
                    "prompt_version": context.prompt_version,
                    "model_revision": revision,
                    "prompt_hash": prompt_hash,
                    "schema_hash": schema_hash,
                    "memory_generation": context.memory_generation,
                    "rag_generation": context.rag_index_generation,
                    "tool_schema": context.tool_schema_version,
                    "context_builder": context.context_builder_version,
                }
            )
            if self.policy.allow_cache(context):
                cached = await self.cache.get(cache_key)
                if cached is not None:
                    result = schema.model_validate(cached)
                    cost_exceeded = await self._audit(
                        context=context,
                        route=route,
                        prompt_hash=prompt_hash,
                        schema_hash=schema_hash,
                        input_tokens=input_tokens,
                        result=result,
                        latency_ms=0,
                        attempts=1,
                        fallback_from=None,
                        status="cache_hit",
                        error=None,
                    )
                    if cost_exceeded:
                        raise ModelCostBudgetExceeded(
                            "cached model invocation exceeded its cost budget"
                        )
                    return result

            started = time.perf_counter()
            attempts_for_model = 0
            for retry in range(max_retries + 1):
                attempts_for_model += 1
                total_attempts += 1
                try:
                    async with asyncio.timeout(context.timeout_seconds):
                        raw = await _structured(candidate, schema).ainvoke(
                            messages, config=config, **kwargs
                        )
                    usage: dict[str, int] | None = None
                    if isinstance(raw, dict) and "parsed" in raw:
                        if raw.get("parsing_error") is not None:
                            raise raw["parsing_error"]
                        result = schema.model_validate(raw.get("parsed"))
                        raw_message = raw.get("raw")
                        raw_usage = getattr(raw_message, "usage_metadata", None)
                        if isinstance(raw_usage, dict) and (
                            raw_usage.get("input_tokens") or raw_usage.get("output_tokens")
                        ):
                            usage = {
                                "input_tokens": int(raw_usage.get("input_tokens", 0)),
                                "output_tokens": int(raw_usage.get("output_tokens", 0)),
                            }
                    else:
                        result = schema.model_validate(raw)
                    self._failures[provider] = 0
                    latency_ms = (time.perf_counter() - started) * 1000
                    if self.policy.allow_cache(context):
                        await self.cache.put(cache_key, result.model_dump(mode="json"))
                    cost_exceeded = await self._audit(
                        context=context,
                        route=route,
                        prompt_hash=prompt_hash,
                        schema_hash=schema_hash,
                        input_tokens=input_tokens,
                        result=result,
                        latency_ms=latency_ms,
                        attempts=total_attempts,
                        fallback_from=(primary_identity[1] if candidate_index else None),
                        status="succeeded",
                        error=None,
                        provider_usage=usage,
                    )
                    if cost_exceeded:
                        raise ModelCostBudgetExceeded("model invocation exceeded its cost budget")
                    return result
                except ModelCostBudgetExceeded:
                    raise
                except Exception as exc:
                    last_error = exc
                    if retry >= max_retries or not _retryable(exc):
                        break
                    await asyncio.sleep(min(0.1 * (2**retry), 0.5))
            self._failures[provider] = self._failures.get(provider, 0) + 1
            if self._failures[provider] >= 5:
                self._open_until[provider] = time.monotonic() + 30
            if candidate_index + 1 == len(models):
                latency_ms = (time.perf_counter() - started) * 1000
                await self._audit(
                    context=context,
                    route=route,
                    prompt_hash=prompt_hash,
                    schema_hash=schema_hash,
                    input_tokens=input_tokens,
                    result=None,
                    latency_ms=latency_ms,
                    attempts=max(total_attempts, attempts_for_model),
                    fallback_from=(primary_identity[1] if candidate_index else None),
                    status="failed",
                    error=last_error,
                    provider_usage=None,
                )
        assert last_error is not None
        raise last_error

    async def _audit(
        self,
        *,
        context: ModelCallContext,
        route: ModelRouteDecision,
        prompt_hash: str,
        schema_hash: str,
        input_tokens: int,
        result: BaseModel | None,
        latency_ms: float,
        attempts: int,
        fallback_from: str | None,
        status: str,
        error: BaseException | None,
        provider_usage: dict[str, int] | None = None,
    ) -> bool:
        estimated_output = (
            len(self._encoding.encode(result.model_dump_json())) if result is not None else 0
        )
        if provider_usage is not None:
            input_tokens = provider_usage["input_tokens"]
            output_tokens = provider_usage["output_tokens"]
            token_source = "provider"
        else:
            output_tokens = estimated_output
            token_source = "cache" if status == "cache_hit" else "estimated"
        input_price, output_price = self.prices.get(route.model, (0.0, 0.0))
        cost = (
            0.0
            if status == "cache_hit"
            else (input_tokens * input_price + output_tokens * output_price) / 1_000_000
        )
        pricing_version = (
            "internal-semantic-cache-v1"
            if status == "cache_hit"
            else "deepseek-2026-08-16-peak-cache-miss"
            if route.model in {"deepseek-v4-flash", "deepseek-v4-pro"}
            else "unconfigured"
        )
        cost_exceeded = context.max_cost_usd is not None and cost > context.max_cost_usd
        await self.audit_sink.record(
            ModelInvocationAudit(
                context=context,
                route=route,
                prompt_hash=prompt_hash,
                schema_hash=schema_hash,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_ms=latency_ms,
                cost_usd=cost,
                token_accounting_source=token_source,
                pricing_version=pricing_version,
                cost_estimate=True,
                attempts=attempts,
                retries=max(attempts - 1, 0),
                fallback_from=fallback_from,
                status="failed" if cost_exceeded else status,
                error_code=(
                    "MODEL_COST_BUDGET_EXCEEDED"
                    if cost_exceeded
                    else _error_code(error)
                    if error
                    else None
                ),
            )
        )
        return cost_exceeded


class GovernedStructuredRunnable[SchemaT: BaseModel]:
    def __init__(
        self,
        gateway: ModelGateway,
        model: BaseChatModel,
        schema: type[SchemaT],
        context: ModelCallContext | None,
        fallback_models: Sequence[BaseChatModel],
    ) -> None:
        self.gateway = gateway
        self.model = model
        self.schema = schema
        self.context = context
        self.fallback_models = fallback_models

    async def ainvoke(self, messages: Any, config: Any = None, **kwargs: Any) -> SchemaT:
        context = (
            self.context
            or _active_context.get()
            or ModelCallContext(
                tenant_id=UUID(int=0),
                agent_role="unscoped",
                purpose=ModelPurpose.ANALYSIS,
                risk=ModelRisk.MEDIUM,
                policy_version="legacy-unscoped-v1",
                prompt_version="legacy-unscoped-v1",
            )
        )
        return await self.gateway.invoke(
            self.model,
            self.schema,
            messages,
            context=context,
            fallback_models=self.fallback_models,
            config=config,
            **kwargs,
        )


_default_model_gateway: ModelGateway | None = None


def default_model_gateway() -> ModelGateway:
    """Return the process-wide ModelGateway, building it lazily on first use.

    Constructing the gateway touches tiktoken's BPE table, which can download
    on first run; deferring construction keeps module import offline and fast.
    """
    global _default_model_gateway
    if _default_model_gateway is None:
        _default_model_gateway = ModelGateway()
    return _default_model_gateway


def governed_structured_output[SchemaT: BaseModel](
    model: BaseChatModel,
    schema: type[SchemaT],
    *,
    context: ModelCallContext | None = None,
    fallback_models: Sequence[BaseChatModel] = (),
) -> GovernedStructuredRunnable[SchemaT]:
    return default_model_gateway().structured(
        model, schema, context=context, fallback_models=fallback_models
    )
