from fastapi import HTTPException

from servicemind.security.auth import TenantContext


def run_scope(context: TenantContext) -> dict[str, list[int]]:
    return {
        "entity_ids": sorted(context.allowed_glpi_entity_ids),
        "group_ids": sorted(context.allowed_glpi_group_ids),
    }


def require_run_access(run, context: TenantContext) -> None:
    if run.tenant_id != context.tenant_id:
        raise HTTPException(status_code=404, detail="Run not found")
    if context.roles.intersection({"operator", "tenant_admin"}):
        return
    scope = getattr(run, "access_scope", None)
    if (
        not isinstance(scope, dict)
        or not isinstance(scope.get("entity_ids"), list)
        or not isinstance(scope.get("group_ids"), list)
        or not set(scope["entity_ids"]).issubset(context.allowed_glpi_entity_ids)
        or not set(scope["group_ids"]).issubset(context.allowed_glpi_group_ids)
        or (run.user_id != context.user_id and "approver" not in context.roles)
    ):
        raise HTTPException(status_code=404, detail="Run not found")
