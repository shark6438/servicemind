from __future__ import annotations

from servicemind.tool_platform.contracts import ToolDefinition


class ToolRegistry:
    """Immutable-version registry; a published name/version cannot be redefined."""

    def __init__(self) -> None:
        self._definitions: dict[tuple[str, str], ToolDefinition] = {}

    def register(self, definition: ToolDefinition) -> None:
        key = (definition.name, definition.version)
        current = self._definitions.get(key)
        if current is not None and current.checksum != definition.checksum:
            raise ValueError(
                f"tool version checksum conflict: {definition.name}@{definition.version}"
            )
        self._definitions[key] = definition

    def resolve(self, name: str, version: str) -> ToolDefinition:
        try:
            definition = self._definitions[(name, version)]
        except KeyError as exc:
            raise LookupError(f"tool is not registered: {name}@{version}") from exc
        if not definition.active:
            raise PermissionError(f"tool is disabled: {name}@{version}")
        return definition

    def visible(
        self, *, roles: frozenset[str], entity_ids: frozenset[int]
    ) -> tuple[ToolDefinition, ...]:
        return tuple(
            definition
            for definition in sorted(
                self._definitions.values(), key=lambda item: (item.name, item.version)
            )
            if definition.active
            and definition.discoverable
            and bool(definition.allowed_roles & roles)
            and (not definition.allowed_entities or bool(definition.allowed_entities & entity_ids))
        )
