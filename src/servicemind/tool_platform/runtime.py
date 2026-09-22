from __future__ import annotations

from functools import lru_cache

from core import settings
from servicemind.tool_platform.audit import InMemoryToolAuditSink, PostgresToolAuditSink
from servicemind.tool_platform.catalog import build_glpi_registry
from servicemind.tool_platform.gateway import ToolGateway
from servicemind.tool_platform.mcp_transport import McpGlpiProvider, StatelessMcpClient
from servicemind.tool_platform.policy import DeterministicToolPolicy, OpaToolPolicy
from servicemind.tool_platform.providers import NativeGlpiProvider
from servicemind.tool_platform.resilience import (
    Bulkheads,
    CircuitBreaker,
    RedisTenantRateLimiter,
    SlidingWindowRateLimiter,
)


@lru_cache(maxsize=1)
def build_tool_gateway() -> ToolGateway:
    provider_name = settings.SERVICEMIND_TOOL_PROVIDER
    if provider_name == "native_glpi":
        provider = NativeGlpiProvider()
    elif provider_name == "mcp_glpi":
        if not settings.SERVICEMIND_MCP_GLPI_URL or not settings.SERVICEMIND_MCP_SERVICE_TOKEN:
            raise RuntimeError("MCP GLPI provider requires URL and service token")

        async def token() -> str:
            assert settings.SERVICEMIND_MCP_SERVICE_TOKEN is not None
            return settings.SERVICEMIND_MCP_SERVICE_TOKEN.get_secret_value()

        provider = McpGlpiProvider(
            StatelessMcpClient(
                settings.SERVICEMIND_MCP_GLPI_URL,
                expected_issuer=settings.SERVICEMIND_MCP_EXPECTED_ISSUER,
                token_provider=token,
            )
        )
    else:
        raise RuntimeError(f"unsupported ServiceMind tool provider: {provider_name}")

    if settings.SERVICEMIND_TOOL_POLICY_MODE == "opa":
        if not settings.SERVICEMIND_OPA_URL:
            raise RuntimeError("OPA policy mode requires SERVICEMIND_OPA_URL")
        policy = OpaToolPolicy(
            settings.SERVICEMIND_OPA_URL,
            decision_path=settings.SERVICEMIND_OPA_DECISION_PATH,
            policy_version=settings.SERVICEMIND_TOOL_POLICY_VERSION,
        )
    elif settings.SERVICEMIND_TOOL_POLICY_MODE == "local":
        policy = DeterministicToolPolicy(policy_version=settings.SERVICEMIND_TOOL_POLICY_VERSION)
    else:
        raise RuntimeError("tool policy mode must be local or opa")

    if settings.SERVICEMIND_REDIS_URL:
        from redis.asyncio import Redis

        redis_client = Redis.from_url(
            settings.SERVICEMIND_REDIS_URL.get_secret_value(),
            decode_responses=False,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        rate_limiter = RedisTenantRateLimiter(
            redis_client, limit=settings.SERVICEMIND_TOOL_RATE_LIMIT_PER_MINUTE
        )
    else:
        rate_limiter = SlidingWindowRateLimiter(
            limit=settings.SERVICEMIND_TOOL_RATE_LIMIT_PER_MINUTE
        )
    audit = (
        PostgresToolAuditSink()
        if settings.DATABASE_TYPE.value == "postgres"
        else InMemoryToolAuditSink()
    )
    return ToolGateway(
        registry=build_glpi_registry(provider_name),
        policy=policy,
        providers={provider.name: provider},
        audit=audit,
        rate_limiter=rate_limiter,
        bulkheads=Bulkheads(settings.SERVICEMIND_TOOL_BULKHEAD_LIMIT),
        circuit_breaker=CircuitBreaker(
            failure_threshold=settings.SERVICEMIND_TOOL_CIRCUIT_FAILURES,
            reset_seconds=settings.SERVICEMIND_TOOL_CIRCUIT_RESET_SECONDS,
        ),
    )
