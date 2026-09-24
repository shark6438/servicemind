from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any
from uuid import UUID

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage
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


def model_error_code(error: BaseException) -> str:
    """Classify a failed invocation for the audit ledger.

    ``PARSER`` is grouped with ``VALIDATION``: a structured call that returns prose or
    malformed JSON is the same defect as one that returns a well-formed object of the
    wrong shape -- the model did not answer the schema -- and the retry has to treat
    them alike or it will replay a question the model has already failed.
    """
    name = type(error).__name__.upper()
    if isinstance(error, TimeoutError):
        return "MODEL_TIMEOUT"
    if "RATE" in name or "429" in str(error):
        return "MODEL_RATE_LIMITED"
    if "VALIDATION" in name or "JSON" in name or "PARSER" in name:
        return "MODEL_SCHEMA_INVALID"
    return f"MODEL_{name[:80]}"


def model_returned_nothing(error: BaseException) -> bool:
    """Whether a structured call failed because the model sent no completion at all.

    Distinct from a schema violation, which the gateway already answers by replaying the
    request with the violation fed back. A schema violation is a property of the answer
    to *these* messages, so re-asking the identical question spends a retry asking for the
    same mistake twice -- that is why the repair instruction exists. An empty completion
    is the opposite: there was no answer to be a property of. The provider returned
    nothing, which is transient by nature -- measured on the 2026-09-24 quality batch,
    case Q-194 raised ``OutputParserException: Failed to parse AnalysisResult from
    completion null`` twice inside one call (the gateway's own retry, then the repair
    retry) and parked the run at ``waiting_review``; the same case re-run twice
    immediately after answered normally, 8.6 seconds, no code change.

    The caller that can afford to wait is the same one ``throttle_wait_seconds`` serves,
    and for the same reason: the run's clock is minutes, the call's is seconds, and a
    failure with no cause in the request is worth one more identical ask.

    Detected on the parser exception's own ``llm_output`` rather than on its message text,
    because the message is langchain's to change and this decision is ours. Kept here
    rather than in the caller for the same reason ``model_error_code`` is: what a failed
    model call *was* is a fact about the model path.
    """
    if model_error_code(error) != "MODEL_SCHEMA_INVALID":
        return False
    return getattr(error, "llm_output", "") in (None, "")


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


#: Ceiling for a transport-class retry. Sub-second is deliberate: a connection reset or
#: a 503 clears on its own, and the caller's deadline is not what is failing.
_TRANSIENT_BACKOFF_CEILING_SECONDS = 0.5

#: Throttling is measured in seconds and is a property of *how recently the account
#: called*, not of the request. These bound the wait when the provider gives no hint.
_RATE_LIMIT_BACKOFF_BASE_SECONDS = 1.0
_RATE_LIMIT_BACKOFF_CEILING_SECONDS = 8.0


def _retry_after_seconds(error: BaseException) -> float | None:
    """Return the provider's own ``Retry-After`` delay in seconds, when it sent one.

    Only the delay-seconds form is read; the HTTP-date form is rare and reading it would
    cost a date parser to improve a wait that already falls back to a bounded one.
    """
    headers = getattr(getattr(error, "response", None), "headers", None)
    if headers is None:
        return None
    try:
        raw = headers.get("retry-after")
    except Exception:  # a header mapping is provider code, not ours
        return None
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return None
    return max(0.0, seconds) if seconds == seconds else None  # reject NaN


def _backoff_seconds(
    error: BaseException, retry: int, *, remaining_seconds: float | None = None
) -> float:
    """Seconds to wait before replaying a failed call, decided by *why* it failed.

    A rate limit is not a transport fault and must not be retried like one. The standard
    schedule here was ``min(0.1 * 2**retry, 0.5)`` for every retryable error, so a 429 was
    replayed 100 ms after the provider refused it -- inside the same throttle window, which
    makes the retry a second identical request rather than a second chance. Measured over
    one day of live acceptance traffic (2026-09-23): nine ``MODEL_RATE_LIMITED`` rows had
    ``attempts=2``, i.e. the retry never once succeeded, and every one of the nine runs
    terminated ``waiting_review`` -- analysis degraded, Reviewer escalated on
    ``DEGRADED_ANALYSIS``. All nine were the analysis call, the largest request the
    platform makes. The wait is bounded by what is left of the call's own timeout budget,
    so honouring a long ``Retry-After`` can never convert a throttle into a timeout.
    """
    if model_error_code(error) == "MODEL_RATE_LIMITED":
        hinted = _retry_after_seconds(error)
        wait = (
            hinted
            if hinted is not None
            else min(
                _RATE_LIMIT_BACKOFF_BASE_SECONDS * (2**retry),
                _RATE_LIMIT_BACKOFF_CEILING_SECONDS,
            )
        )
    else:
        wait = min(0.1 * (2**retry), _TRANSIENT_BACKOFF_CEILING_SECONDS)
    if remaining_seconds is not None:
        wait = min(wait, max(0.0, remaining_seconds))
    return wait


def throttle_wait_seconds(error: BaseException, attempt: int) -> float | None:
    """How long a caller should wait before re-asking, or ``None`` if this is not a throttle.

    Public because the wait has to be possible from outside the gateway's own retry loop.
    The gateway's loop is bounded by the *call's* timeout -- seconds -- and gives up when
    that budget is spent, which is the right thing for a call: a single request may not
    hold a worker open indefinitely. But a throttle outlives it routinely. A provider that
    answers ``Retry-After: 30`` and a call whose timeout is 20 seconds means the loop
    cannot outlast the window it is being asked to wait for, so it exhausts, the model
    path fails, and the failure lands somewhere far from the cause. A caller holding a
    longer clock -- a run with a deadline measured in minutes -- can wait it out, and this
    is what it needs in order to know how long.

    ``None`` and ``0.0`` are different answers and both are reachable. ``0.0`` is what a
    provider asks for when it sends ``Retry-After: 0`` (or a date already past, which
    :func:`_retry_after_seconds` clamps to zero): re-ask immediately, because the window
    it was refusing inside has ended. A caller that read ``0.0`` as "not a throttle" gave
    up on a request the provider had just invited, so the nil hint is the one Retry-After
    that would never be honoured. The distinction has to survive the return value, hence
    an optional rather than a sentinel.
    """
    if model_error_code(error) != "MODEL_RATE_LIMITED":
        return None
    return _backoff_seconds(error, attempt)


#: Appended to the request when a retry follows a schema violation. DeepSeek's
#: ``json_mode`` advertises the schema in the prompt without enforcing it, so a schema
#: violation is a deterministic property of the answer to *these* messages: replaying
#: them byte for byte spends the retry asking the model to make the same mistake twice.
#: The retry only becomes a new question once it carries what was wrong with the answer.
_SCHEMA_REPAIR = (
    "Your previous response did not satisfy the required response schema. The validation "
    "error was:\n\n{error}\n\nAnswer the same request again and return ONLY the JSON object. "
    "Every required field must be present, correctly typed and within its stated bounds; do "
    "not add fields the schema does not define, and do not wrap the object in prose or "
    "markdown fences."
)


def _repair_messages(messages: Any, error: BaseException) -> Any:
    """Return the same request with the schema violation fed back to the model."""
    instruction = _SCHEMA_REPAIR.format(error=str(error)[:1000])
    if isinstance(messages, str):
        return f"{messages}\n\n{instruction}"
    if isinstance(messages, Sequence) and not isinstance(messages, (str, bytes)):
        return [*messages, HumanMessage(content=instruction)]
    return messages


def _conservative_token_count(value: object) -> int:
    """Return a deterministic offline upper bound for BPE-family token counts.

    Provider usage replaces this estimate after a successful call. Counting UTF-8
    bytes is intentionally conservative for pre-provider/cache/failure accounting:
    it cannot silently understate a byte-level BPE token count, needs no model file,
    and never turns a cold process start into an unbounded network download.
    """
    return len(str(value).encode("utf-8"))


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
        input_tokens = _conservative_token_count(payload)
        call_started = time.perf_counter()
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
            # What the next attempt sends. It stays the caller's own request until an
            # attempt fails on the schema, and only then carries the repair instruction.
            # ``prompt_hash`` and the cache key were computed from the caller's request
            # above and are deliberately not re-derived here: a repair that succeeds is
            # still an answer to the original question, so it belongs under the original
            # key. That a repair happened stays visible in the audit ``attempts`` count.
            attempt_messages = messages
            for retry in range(max_retries + 1):
                attempts_for_model += 1
                total_attempts += 1
                try:
                    async with asyncio.timeout(context.timeout_seconds):
                        raw = await _structured(candidate, schema).ainvoke(
                            attempt_messages, config=config, **kwargs
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
                    if model_error_code(exc) == "MODEL_SCHEMA_INVALID":
                        attempt_messages = _repair_messages(messages, exc)
                    await asyncio.sleep(
                        _backoff_seconds(
                            exc,
                            retry,
                            remaining_seconds=context.timeout_seconds
                            - (time.perf_counter() - call_started),
                        )
                    )
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
            _conservative_token_count(result.model_dump_json()) if result is not None else 0
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
                    else model_error_code(error)
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
    """Return the process-wide governed model gateway."""
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
