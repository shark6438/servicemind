from __future__ import annotations

import json
from collections.abc import Iterable

from servicemind.model_gateway.contracts import ModelCallContext, ModelRisk, ModelRouteDecision


class ModelRoutePolicy:
    def __init__(
        self,
        *,
        allowed_providers: Iterable[str] = ("deepseek", "openai", "azure", "fake"),
        allowed_models: Iterable[str] = ("*",),
        tenant_allowlists: dict[str, dict[str, list[str]]] | None = None,
    ) -> None:
        self.allowed_providers = frozenset(value.casefold() for value in allowed_providers)
        self.allowed_models = frozenset(allowed_models)
        self.tenant_allowlists = tenant_allowlists or {}

    def decide(
        self,
        *,
        provider: str,
        model: str,
        revision: str,
        context: ModelCallContext | None = None,
    ) -> ModelRouteDecision:
        providers = self.allowed_providers
        models = self.allowed_models
        entry = None
        if context is not None and context.tenant_id.int != 0 and self.tenant_allowlists:
            entry = self.tenant_allowlists.get(str(context.tenant_id))
            if entry is None:
                raise PermissionError(f"tenant has no model allowlist entry: {context.tenant_id}")
        if entry is not None:
            providers &= frozenset(value.casefold() for value in entry.get("providers", []))
            models &= frozenset(entry.get("models", []))
        if provider.casefold() not in providers:
            raise PermissionError(f"model provider is not allowlisted: {provider}")
        if "*" not in models and model not in models:
            raise PermissionError(f"model is not allowlisted: {model}")
        return ModelRouteDecision(
            provider=provider,
            model=model,
            model_revision=revision,
            reason="tenant_and_runtime_allowlist_passed",
        )

    @staticmethod
    def allow_fallback(context: ModelCallContext) -> bool:
        return context.risk not in {ModelRisk.HIGH, ModelRisk.CRITICAL}

    @staticmethod
    def allow_cache(context: ModelCallContext) -> bool:
        return (
            context.cache_allowed
            and not context.personal_or_volatile
            and context.risk not in {ModelRisk.HIGH, ModelRisk.CRITICAL}
            and context.purpose.value not in {"review", "memory_extraction"}
        )


class SettingsModelRoutePolicy(ModelRoutePolicy):
    """Resolve allowlists at call time so configuration reloads remain fail-closed."""

    def decide(
        self,
        *,
        provider: str,
        model: str,
        revision: str,
        context: ModelCallContext | None = None,
    ) -> ModelRouteDecision:
        from core import settings

        tenant_allowlists: dict[str, dict[str, list[str]]] = json.loads(
            settings.SERVICEMIND_TENANT_MODEL_ALLOWLIST_JSON
        )
        policy = ModelRoutePolicy(
            allowed_providers=(
                value.strip()
                for value in settings.SERVICEMIND_MODEL_ALLOWED_PROVIDERS.split(",")
                if value.strip()
            ),
            allowed_models=(
                value.strip()
                for value in settings.SERVICEMIND_MODEL_ALLOWED_MODELS.split(",")
                if value.strip()
            ),
            tenant_allowlists=tenant_allowlists,
        )
        return policy.decide(
            provider=provider,
            model=model,
            revision=revision,
            context=context,
        )

    def allow_cache(self, context: ModelCallContext) -> bool:
        from core import settings

        return settings.SERVICEMIND_SEMANTIC_CACHE_ENABLED and super().allow_cache(context)
