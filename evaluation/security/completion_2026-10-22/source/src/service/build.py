import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from core import settings
from servicemind.evaluation.source_revision import source_revision


def capture_build_identity() -> dict:
    configuration = {
        "default_model": str(settings.DEFAULT_MODEL),
        "ollama_model": settings.OLLAMA_MODEL,
        "compatible_model": settings.COMPATIBLE_MODEL,
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
