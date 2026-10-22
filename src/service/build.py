import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from core import settings
from servicemind.evaluation.source_revision import source_revision


def endpoint_fingerprint(value: str | None) -> str:
    parsed = urlsplit(value or "")
    endpoint = [parsed.scheme, parsed.hostname, parsed.port, parsed.path]
    return hashlib.sha256(json.dumps(endpoint, separators=(",", ":")).encode()).hexdigest()


def capture_build_identity() -> dict:
    configuration = {
        "default_model": str(settings.DEFAULT_MODEL),
        "ollama_model": settings.OLLAMA_MODEL,
        "compatible_model": settings.COMPATIBLE_MODEL,
        "compatible_endpoint_sha256": endpoint_fingerprint(settings.COMPATIBLE_BASE_URL),
        "model_timeout_seconds": settings.SERVICEMIND_MODEL_TIMEOUT_SECONDS,
        "model_max_retries": settings.SERVICEMIND_MODEL_MAX_RETRIES,
        "data_planning_timeout_seconds": settings.SERVICEMIND_DATA_PLANNING_TIMEOUT_SECONDS,
        "retrieval_rewrite_timeout_seconds": settings.SERVICEMIND_RETRIEVAL_REWRITE_TIMEOUT_SECONDS,
        "model_trace_headers_enabled": settings.SERVICEMIND_MODEL_TRACE_HEADERS_ENABLED,
        "semantic_cache_enabled": settings.SERVICEMIND_SEMANTIC_CACHE_ENABLED,
        "context_max_input_tokens": settings.SERVICEMIND_CONTEXT_MAX_INPUT_TOKENS,
        "context_evidence_token_cap": settings.SERVICEMIND_CONTEXT_EVIDENCE_TOKEN_CAP,
        "product_database_endpoint_sha256": endpoint_fingerprint(
            settings.SERVICEMIND_DATABASE_URL.get_secret_value()
            if settings.SERVICEMIND_DATABASE_URL
            else None
        ),
        "checkpoint_database_endpoint_sha256": endpoint_fingerprint(
            f"postgresql://{settings.POSTGRES_HOST}:{settings.POSTGRES_PORT}/{settings.POSTGRES_DB}"
            if str(settings.DATABASE_TYPE) == "postgres"
            else str(settings.DATABASE_TYPE)
        ),
        "model_allowed_providers": settings.SERVICEMIND_MODEL_ALLOWED_PROVIDERS,
        "model_allowed_models": settings.SERVICEMIND_MODEL_ALLOWED_MODELS,
        "generic_api_enabled": settings.SERVICEMIND_GENERIC_API_ENABLED,
        "oidc_audience": settings.SERVICEMIND_OIDC_AUDIENCE,
        "tool_platform_enabled": settings.SERVICEMIND_TOOL_PLATFORM_ENABLED,
        "tool_provider": settings.SERVICEMIND_TOOL_PROVIDER,
    }
    return {
        "source_revision": source_revision(Path(__file__).resolve().parents[2]),
        "boot_id": str(uuid4()),
        "captured_at": datetime.now(UTC).isoformat(),
        "configuration": configuration,
        "configuration_sha256": hashlib.sha256(
            json.dumps(configuration, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }
