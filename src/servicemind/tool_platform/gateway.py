from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Mapping
from typing import Any

import httpx
from jsonschema import Draft202012Validator

from servicemind.tool_platform.audit import (
    InMemoryToolAuditSink,
    ToolAuditSink,
    ToolInvocationAudit,
)
from servicemind.tool_platform.contracts import (
    ProviderResult,
    ToolAccess,
    ToolCall,
    ToolExecutionResult,
    ToolPolicyEngine,
    ToolProvider,
    output_hash,
)
from servicemind.tool_platform.registry import ToolRegistry
from servicemind.tool_platform.resilience import (
    Bulkheads,
    CircuitBreaker,
    CircuitOpen,
    IdempotencyLedger,
    RateLimitExceeded,
    SlidingWindowRateLimiter,
    TenantRateLimiter,
)


class ToolPolicyDenied(PermissionError):
    pass


class ToolContractViolation(ValueError):
    pass


class ToolVerificationFailed(RuntimeError):
    pass


def _validate(schema: dict[str, Any], value: Any, boundary: str) -> None:
    Draft202012Validator.check_schema(schema)
    errors = sorted(
        Draft202012Validator(schema).iter_errors(value), key=lambda item: list(item.path)
    )
    if errors:
        raise ToolContractViolation(f"{boundary} schema rejected value at {list(errors[0].path)}")


def _failure_code(exc: BaseException) -> str:
    if isinstance(exc, TimeoutError | asyncio.TimeoutError | httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.HTTPStatusError):
        if exc.response.status_code == 429:
            return "rate_limit"
        if exc.response.status_code >= 500:
            return "provider_unavailable"
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        if status_code == 429:
            return "rate_limit"
        if status_code >= 500:
            return "provider_unavailable"
        if status_code in {401, 403}:
            return "provider_authorization_denied"
        if status_code == 404:
            return "provider_not_found"
        if status_code == 409:
            return "provider_conflict"
        if status_code >= 400:
            return "provider_rejected"
    if isinstance(exc, ToolVerificationFailed):
        return "verification_failed"
    if isinstance(exc, ToolContractViolation):
        return "contract_violation"
    if isinstance(exc, RateLimitExceeded):
        return "rate_limit"
    if isinstance(exc, CircuitOpen):
        return "circuit_open"
    if isinstance(exc, PermissionError):
        return "authorization_scope_denied"
    return "provider_error"


class ToolGateway:
    """The sole execution boundary for native and MCP tool providers."""

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        policy: ToolPolicyEngine,
        providers: Mapping[str, ToolProvider],
        audit: ToolAuditSink | None = None,
        rate_limiter: TenantRateLimiter | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        bulkheads: Bulkheads | None = None,
        idempotency: IdempotencyLedger | None = None,
    ) -> None:
        self.registry, self.policy = registry, policy
        self.providers = dict(providers)
        self.audit = audit or InMemoryToolAuditSink()
        self.rate_limiter = rate_limiter or SlidingWindowRateLimiter()
        self.circuit = circuit_breaker or CircuitBreaker()
        self.bulkheads = bulkheads or Bulkheads()
        self.idempotency = idempotency or IdempotencyLedger()

    async def execute(self, call: ToolCall) -> ToolExecutionResult:
        started = time.perf_counter()
        definition = self.registry.resolve(call.tool_name, call.tool_version)
        _validate(definition.input_schema, call.arguments, "input")
        decision = await self.policy.decide(definition, call)
        await self.audit.record_policy(definition, call, decision)
        if not decision.allow:
            await self._audit(
                definition,
                call,
                decision.decision_id,
                started,
                status="denied",
                attempts=0,
                error_code="policy_denied",
            )
            raise ToolPolicyDenied(",".join(decision.reason_codes))
        unexpected = set(call.arguments) - set(decision.allowed_fields)
        if unexpected:
            await self._audit(
                definition,
                call,
                decision.decision_id,
                started,
                status="denied",
                attempts=0,
                error_code="field_denied",
            )
            raise ToolPolicyDenied(f"policy denied fields: {sorted(unexpected)}")
        try:
            provider = self.providers[definition.provider]
        except KeyError as exc:
            await self._audit(
                definition,
                call,
                decision.decision_id,
                started,
                status="failed",
                attempts=0,
                error_code="provider_unavailable",
            )
            raise RuntimeError(f"tool provider is unavailable: {definition.provider}") from exc

        try:
            await self.rate_limiter.acquire(call.tenant_id, definition.name)
            circuit_key = f"{definition.provider}:{definition.name}"
            await self.circuit.before(circuit_key)
        except Exception as exc:
            await self._audit(
                definition,
                call,
                decision.decision_id,
                started,
                status="failed",
                attempts=0,
                error_code=_failure_code(exc),
            )
            raise
        key = f"{definition.name}:{definition.version}:{call.request_id}"
        try:
            lock = await self.idempotency.lock(call.tenant_id, key, call.argument_hash)
        except Exception:
            await self._audit(
                definition,
                call,
                decision.decision_id,
                started,
                status="failed",
                attempts=0,
                error_code="idempotency_conflict",
            )
            raise
        async with lock, self.bulkheads.for_tool(definition.name):
            cached = self.idempotency.get(call.tenant_id, key)
            if cached is not None:
                return cached.model_copy(update={"duplicate_suppressed": True})
            attempts = 0
            try:
                result: ProviderResult | None = None
                for attempts in range(1, definition.retry_policy.max_attempts + 1):
                    call.ensure_active()
                    try:
                        result = await asyncio.wait_for(
                            provider.execute(definition, call),
                            timeout=min(
                                definition.timeout_seconds,
                                max((call.deadline.timestamp() - time.time()), 0.001),
                            ),
                        )
                        break
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        code = _failure_code(exc)
                        if (
                            definition.read_write_type is not ToolAccess.READ
                            or attempts >= definition.retry_policy.max_attempts
                            or code not in definition.retry_policy.retryable_codes
                        ):
                            raise
                        ceiling = min(
                            definition.retry_policy.base_delay_ms * (2 ** (attempts - 1)),
                            definition.retry_policy.max_delay_ms,
                        )
                        await asyncio.sleep(random.uniform(0, ceiling) / 1000)
                assert result is not None
                _validate(definition.output_schema, result.output, "output")
                verified = await asyncio.wait_for(
                    provider.verify(definition, call, result),
                    timeout=min(definition.timeout_seconds, 10),
                )
                if not verified:
                    raise ToolVerificationFailed("provider read-back verification failed")
                execution = ToolExecutionResult(
                    request_id=call.request_id,
                    tool_name=definition.name,
                    tool_version=definition.version,
                    provider=definition.provider,
                    output=result.output,
                    output_hash=output_hash(result.output),
                    verified=True,
                    attempts=attempts,
                    policy_decision_id=decision.decision_id,
                )
                self.idempotency.complete(call.tenant_id, key, execution)
                await self.circuit.success(circuit_key)
                await self._audit(
                    definition,
                    call,
                    decision.decision_id,
                    started,
                    status="succeeded",
                    attempts=attempts,
                    output_hash_value=execution.output_hash,
                    verified=True,
                )
                return execution
            except asyncio.CancelledError:
                await self.idempotency.abort(call.tenant_id, key)
                await self._audit(
                    definition,
                    call,
                    decision.decision_id,
                    started,
                    status="cancelled",
                    attempts=attempts,
                    error_code="cancelled",
                )
                raise
            except Exception as exc:
                await self.idempotency.abort(call.tenant_id, key)
                await self.circuit.failure(circuit_key)
                await self._audit(
                    definition,
                    call,
                    decision.decision_id,
                    started,
                    status="failed",
                    attempts=attempts,
                    error_code=_failure_code(exc),
                )
                raise

    async def _audit(
        self,
        definition,
        call: ToolCall,
        decision_id,
        started: float,
        *,
        status: str,
        attempts: int,
        error_code: str | None = None,
        output_hash_value: str | None = None,
        verified: bool = False,
    ) -> None:
        await self.audit.record_invocation(
            ToolInvocationAudit(
                request_id=call.request_id,
                tenant_id=call.tenant_id,
                run_id=call.run_id,
                task_id=call.task_id,
                user_id=call.user_id,
                tool_name=definition.name,
                tool_version=definition.version,
                tool_checksum=definition.checksum,
                provider=definition.provider,
                argument_hash=call.argument_hash,
                output_hash=output_hash_value,
                policy_decision_id=decision_id,
                status=status,
                verified=verified,
                attempts=attempts,
                latency_ms=(time.perf_counter() - started) * 1000,
                error_code=error_code,
            )
        )
