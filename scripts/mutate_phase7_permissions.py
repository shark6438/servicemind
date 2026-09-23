"""Revert each fix in turn and require the suite to notice.

A test that passes both with and without the behaviour it claims to protect has no
teeth, and this path has produced exactly that before: an earlier version of these
tests asserted on ``configurable``, which nothing reads, so they were green while the
narrowing they described was being discarded. Every mutation below reverts one
decision the design actually rests on.

Equivalent mutants are called out where they were found, because an equivalent mutant
that reads RED is a false sense of safety and an equivalent mutant left GREEN looks
like a hole.
"""

import atexit
import json
import os
import pathlib
import signal
import subprocess
import sys
from pathlib import Path

ROOT = pathlib.Path(__file__).resolve().parent.parent
#: Where the pristine snapshot of the last run is kept. Written before the first
#: mutation and read before the next run, so a tree left dirty by ``SIGKILL`` -- the
#: one signal no handler can catch -- is reported rather than graded.
#: ``.runtime/`` rather than the repo root: it is already ignored, so the snapshot
#: neither shows up as a working-tree change nor is read as a scaffold file by
#: ``scripts/audit_project_structure.py``.
MANIFEST = ROOT / ".runtime" / "mutate_phase7_permissions.baseline.json"
LOCK = ROOT / ".runtime" / "mutate_phase7_permissions.lock"
RUNTIME = ROOT / "src/servicemind/orchestration/runtime.py"
SUPERVISOR = ROOT / "src/servicemind/orchestration/supervisor_workflow.py"
RECOVERY = ROOT / "src/servicemind/orchestration/recovery.py"
ENTITLEMENTS = ROOT / "src/servicemind/security/entitlements.py"
AUTH = ROOT / "src/servicemind/security/auth.py"
API = ROOT / "src/servicemind/api.py"
RESOLVER = ROOT / "src/servicemind/integrations/glpi/resolver.py"
REGISTRY = ROOT / "src/servicemind/tool_platform/registry.py"
PROJECTION = ROOT / "src/servicemind/graphrag/projection.py"
GRAPH_STORE = ROOT / "src/servicemind/graphrag/store.py"
GRAPH_RETRIEVAL = ROOT / "src/servicemind/graphrag/retrieval.py"
NEO4J = ROOT / "src/servicemind/graphrag/neo4j.py"
RAG_SERVICE = ROOT / "src/servicemind/rag/service.py"
TOOL_GATEWAY = ROOT / "src/servicemind/runtime/tool_gateway.py"

TEST = "tests/servicemind/test_phase7_principal_invariants.py"
RECOVERY_TEST = "tests/servicemind/test_recovery.py"
RT = "tests/servicemind/test_supervisor_runtime.py"
GRAPH_TEST = "tests/servicemind/test_phase4_graphrag.py"
SUBAGENTS = "tests/servicemind/test_enterprise_subagents.py"

MUTATIONS = [
    # ---- B1: the group carrier ------------------------------------------------------
    (
        "M01 start_run stops writing the group carrier",
        RUNTIME,
        "        group_ids=sorted(context.allowed_glpi_group_ids),\n",
        "",
        f"{TEST}::test_start_run_carries_group_scope_into_the_graph",
    ),
    (
        "M02 a missing group coordinate reads as unrestricted",
        SUPERVISOR,
        'allowed_glpi_group_ids=set(state.get("group_ids") or ()),',
        "allowed_glpi_group_ids={1, 2, 3, 4},",
        f"{TEST}::test_the_graph_rebuilds_group_scope_from_state "
        f"{TEST}::test_a_checkpoint_without_group_scope_reads_as_no_group_scope",
    ),
    # ---- B1: whose authority the resume spends --------------------------------------
    (
        "M03 the resume runs as the caller again (the original escalation)",
        RUNTIME,
        "        Command(resume=_resume_payload(decision, scope.void), update=scope.update),\n"
        "        config=run_config(run, scope.effective),",
        "        Command(resume=_resume_payload(decision, scope.void), update=scope.update),\n"
        "        config=run_config(run, context),",
        f"{TEST}::test_approving_does_not_lend_the_approver_scope",
    ),
    (
        "M04 a checkpoint from another tenant is accepted",
        RUNTIME,
        "    if recorded_tenant is None or UUID(str(recorded_tenant)) != context.tenant_id:\n"
        '        raise PermissionError("run checkpoint belongs to a different tenant")',
        "    if recorded_tenant is None:\n"
        '        raise PermissionError("run checkpoint belongs to a different tenant")',
        f"{TEST}::test_a_checkpoint_from_another_tenant_cannot_be_resumed",
    ),
    (
        "M05 a run with no checkpoint resumes as the caller",
        RUNTIME,
        '        raise PermissionError("cannot resume a run with no checkpointed principal")',
        "        return context",
        f"{TEST}::test_a_run_with_no_checkpoint_cannot_be_resumed",
    ),
    # ---- B1: the intersection is computed but not used ------------------------------
    (
        "M06 the intersection is handed over as configurable only (the original bug)",
        RUNTIME,
        "        Command(resume=_resume_payload(decision, scope.void), update=scope.update),\n",
        "        Command(resume=_resume_payload(decision, scope.void)),\n",
        f"{TEST}::test_a_revoked_group_is_enforced_into_the_checkpoint",
    ),
    (
        "M07 a mid-run recovery skips aupdate_state",
        RUNTIME,
        "        await supervisor_graph.aupdate_state(run_config(run, scope.effective), scope.update)\n",
        "",
        f"{TEST}::test_a_mid_run_recovery_enforces_the_intersection_into_the_checkpoint",
    ),
    (
        "M08 the intersection is replaced by the current grants alone",
        RUNTIME,
        "        roles=intersect(recorded.roles, result.roles),\n"
        "        allowed_glpi_entity_ids=intersect(recorded.allowed_glpi_entity_ids, result.entity_ids),\n"
        "        allowed_glpi_group_ids=intersect(recorded.allowed_glpi_group_ids, result.group_ids),",
        "        roles=set(result.roles),\n"
        "        allowed_glpi_entity_ids=set(result.entity_ids),\n"
        "        allowed_glpi_group_ids=set(result.group_ids),",
        f"{TEST}::test_a_resume_that_narrows_nothing_writes_no_audit",
    ),
    (
        "M09 the intersection is replaced by the recorded grants alone",
        RUNTIME,
        "        roles=intersect(recorded.roles, result.roles),\n"
        "        allowed_glpi_entity_ids=intersect(recorded.allowed_glpi_entity_ids, result.entity_ids),\n"
        "        allowed_glpi_group_ids=intersect(recorded.allowed_glpi_group_ids, result.group_ids),",
        "        roles=set(recorded.roles),\n"
        "        allowed_glpi_entity_ids=set(recorded.allowed_glpi_entity_ids),\n"
        "        allowed_glpi_group_ids=set(recorded.allowed_glpi_group_ids),",
        f"{TEST}::test_a_revoked_group_is_enforced_into_the_checkpoint",
    ),
    (
        "M10 narrowing leaves the wider scope's evidence in place",
        RUNTIME,
        '        update["knowledge_evidence"] = []\n        update["data_evidence"] = []\n',
        "",
        f"{TEST}::test_narrowing_invalidates_evidence_gathered_under_the_wider_scope",
    ),
    # ---- B1: the three outcomes that must not be collapsed --------------------------
    (
        "M11 an unestablished authority narrows and continues instead of pausing",
        RUNTIME,
        "        blocked = ResumeBlocked(result.outcome.value, result.detail)\n"
        "        await _record_block(run, recorded.tenant_id, blocked)\n"
        "        raise blocked",
        "        result = EntitlementResult(EntitlementOutcome.VERIFIED)",
        f"{TEST}::test_an_unestablished_authority_pauses_rather_than_narrows",
    ),
    (
        "M12 a deleted or disabled subject is narrowed rather than refused",
        ENTITLEMENTS,
        "    if not result.established:\n"
        "        raise AuthorityWithdrawn(result.outcome.value, result.detail)",
        "    if result.outcome is EntitlementOutcome.UNAVAILABLE:\n"
        "        raise AuthorityWithdrawn(result.outcome.value, result.detail)",
        f"{TEST}::test_the_write_is_refused_when_the_grants_cannot_be_established",
    ),
    (
        "M13 an observed-empty grant set is treated as a failure to answer",
        ENTITLEMENTS,
        "        return self.outcome is EntitlementOutcome.VERIFIED",
        "        return self.outcome is EntitlementOutcome.VERIFIED and bool(self.roles)",
        f"{TEST}::test_an_established_but_empty_grant_set_is_not_a_failure",
    ),
    (
        "M14 the resume boundary stops auditing why it paused",
        RUNTIME,
        "        await _record_block(run, recorded.tenant_id, blocked)\n",
        "",
        f"{TEST}::test_an_unestablished_authority_pauses_rather_than_narrows",
    ),
    # ---- B1: the cache window -------------------------------------------------------
    (
        "M15 a write-requesting resume is served from the cache",
        RUNTIME,
        "fresh=run.request_write)",
        "fresh=False)",
        f"{TEST}::test_a_write_requester_revalidation_bypasses_the_cache",
    ),
    (
        "M16 the realm reader caches a failure as if it were an answer",
        ENTITLEMENTS,
        "        if result.established or result.outcome in (\n"
        "            EntitlementOutcome.USER_UNKNOWN,\n"
        "            EntitlementOutcome.USER_DISABLED,\n"
        "        ):",
        "        if True:",
        f"{TEST}::test_the_realm_reader_maps_each_answer_to_its_own_outcome",
    ),
    (
        "M17 an HTTP 404 is read as a server error instead of an unknown subject",
        ENTITLEMENTS,
        "            if user.status_code == 404:\n"
        "                return EntitlementResult(\n"
        '                    EntitlementOutcome.USER_UNKNOWN, detail="subject is not in the realm"\n'
        "                )\n",
        "",
        f"{TEST}::test_the_realm_reader_maps_each_answer_to_its_own_outcome",
    ),
    (
        "M18 a disabled subject is read as an enabled one",
        ENTITLEMENTS,
        '            if not payload.get("enabled", True):\n',
        "            if False:\n",
        f"{TEST}::test_the_realm_reader_maps_each_answer_to_its_own_outcome",
    ),
    (
        "M19 the subject is addressed by name instead of by immutable id",
        ENTITLEMENTS,
        "            result = await self._fetch(user_id)",
        "            result = await self._fetch(self._username)",
        f"{TEST}::test_the_realm_reader_maps_each_answer_to_its_own_outcome",
    ),
    (
        "M20 an unreachable realm is read as an empty grant set",
        ENTITLEMENTS,
        "            result = EntitlementResult(\n"
        '                EntitlementOutcome.UNAVAILABLE, detail=f"{type(exc).__name__}"\n'
        "            )",
        "            result = EntitlementResult(EntitlementOutcome.VERIFIED)",
        f"{TEST}::test_an_unreachable_realm_is_unavailable_not_empty",
    ),
    (
        "M27 a cached answer is not marked as cached",
        ENTITLEMENTS,
        "                return replace(\n"
        "                    cached[1],\n"
        "                    from_cache=True,\n"
        "                    cache_window_seconds=self._cache_seconds,\n"
        "                )",
        "                return cached[1]",
        f"{TEST}::test_the_realm_reader_maps_each_answer_to_its_own_outcome",
    ),
    (
        "M28 the revocation window the decision used is not recorded",
        RUNTIME,
        '            "answer_source": "cache" if scope.answer.from_cache else "identity_provider",',
        '            "answer_source": "identity_provider",',
        f"{TEST}::test_the_revocation_window_a_decision_used_is_recorded",
    ),
    # ---- B1: the point of use -------------------------------------------------------
    (
        "M21 execute_node stops re-checking before the write",
        SUPERVISOR,
        "            await require_still_held(\n",
        "            await (lambda **_: None)(\n",
        f"{RT}::test_a_grant_revoked_after_approval_stops_the_write",
    ),
    (
        "M22 the pre-write check does not bypass the cache",
        ENTITLEMENTS,
        "    result = await revalidate(tenant_id, user_id, fresh=True)\n"
        "    if not result.established:",
        "    result = await revalidate(tenant_id, user_id, fresh=False)\n"
        "    if not result.established:",
        f"{TEST}::test_the_write_proceeds_when_every_grant_is_still_held",
    ),
    # ---- B1: recovery ---------------------------------------------------------------
    (
        "M23 recovery invents a service principal again",
        RECOVERY,
        "        user_id=run.user_id,\n"
        "        username=run.user_id,\n"
        "        allowed_glpi_entity_ids={integration.entity_id},",
        "        user_id=run.user_id,\n"
        "        username=run.user_id,\n"
        '        roles={"viewer", "analyst"},\n'
        "        allowed_glpi_group_ids={1, 2, 3, 4},\n"
        "        allowed_glpi_entity_ids={integration.entity_id},",
        f"{RECOVERY_TEST}::test_recovery_hands_over_identity_and_nothing_else",
    ),
    (
        "M24 recovery always claims the checkpoint was the source",
        RECOVERY,
        "                if await get_checkpoint_state(run, context)\n"
        '                else "verified_current_grants"',
        '                if True\n                else "verified_current_grants"',
        f"{RECOVERY_TEST}::test_recovery_hands_over_identity_and_nothing_else",
    ),
    (
        "M25 a paused run is marked FAILED",
        RECOVERY,
        "            except ResumeBlocked:\n",
        "            except ZeroDivisionError:\n",
        f"{RECOVERY_TEST}::test_a_paused_run_is_neither_recovered_nor_failed",
    ),
    # ---- B1: the deployment wiring --------------------------------------------------
    (
        "M26 the deployment wiring never installs a verifier",
        AUTH,
        "    verifier = _entitlement_verifier()\n"
        "    configure_entitlement_verifier(verifier)\n"
        "    return verifier",
        "    return _entitlement_verifier()",
        f"{TEST}::test_a_configured_deployment_installs_a_keycloak_verifier",
    ),
    # ---- the order of persisting and of applying, at both resume boundaries ---------
    (
        "M29 the approval is recorded before the resume that can refuse it",
        API,
        '    refusal = request.decision == "rejected"\n'
        "    scope: ResumeScope | None = None\n"
        "    if not refusal:\n"
        "        try:\n"
        "            scope = await resolve_resume_scope(run, context)\n"
        "        except ResumeBlocked as exc:",
        '    refusal = request.decision == "rejected"\n'
        "    scope: ResumeScope | None = None\n"
        "    if not refusal:\n"
        "        try:\n"
        "            await repository.record_approval(\n"
        "                run_id=run_id,\n"
        "                action_intent_id=action.id,\n"
        "                decision=request.decision,\n"
        "                decided_by=context.user_id,\n"
        "                comment=request.comment,\n"
        "            )\n"
        "            scope = await resolve_resume_scope(run, context)\n"
        "        except ResumeBlocked as exc:",
        f"{TEST}::test_a_paused_resume_records_no_approval",
    ),
    (
        "M30 the escalation audit and status flip land before the scope is resolved",
        API,
        "    try:\n"
        "        scope = await resolve_resume_scope(run, context)\n"
        "    except ResumeBlocked as exc:\n"
        "        # The run stays in WAITING_REVIEW and nothing is written; the human's answer is\n"
        "        # still theirs to give.",
        "    try:\n"
        "        await repository.update_run(run.id, RunStatus.RUNNING)\n"
        "        scope = await resolve_resume_scope(run, context)\n"
        "    except ResumeBlocked as exc:\n"
        "        # The run stays in WAITING_REVIEW and nothing is written; the human's answer is\n"
        "        # still theirs to give.",
        f"{TEST}::test_a_paused_review_resolution_writes_nothing",
    ),
    # ---- the pending action at the resume boundary ----------------------------------
    (
        "M31 a narrowed action is re-scoped and carried on instead of withdrawn",
        RUNTIME,
        '    void = narrowed and bool(values.get("action_intent"))\n',
        "    void = False\n",
        f"{TEST}::test_a_pending_action_is_withdrawn_when_the_scope_narrows",
    ),
    (
        "M32 the void is announced but not carried into the resume payload",
        RUNTIME,
        '    return {"voided": True} if void else decision.model_dump()',
        "    return decision.model_dump()",
        f"{TEST}::test_a_pending_action_is_withdrawn_when_the_scope_narrows",
    ),
    (
        "M33 the conclusions drawn from the cleared evidence are left standing",
        RUNTIME,
        "        for product in _DERIVED_PRODUCTS:\n            update[product] = None\n",
        "",
        f"{TEST}::test_a_pending_action_is_withdrawn_when_the_scope_narrows",
    ),
    (
        "M34 the step's own role requirement is not checked before the approval",
        RUNTIME,
        "            required_roles=ACTION_REQUIRED_ROLES,\n        )\n        if missing:",
        "            required_roles=frozenset(),\n        )\n        if missing:",
        f"{TEST}::test_a_requester_who_never_held_the_step_role_cannot_approve_it",
    ),
    (
        "M35 the approval node records a void as the decision it arrived with",
        SUPERVISOR,
        '        if isinstance(decision, dict) and decision.get("voided"):\n',
        "        if False:\n",
        f"{RT}::test_a_voided_action_is_re_derived_rather_than_decided",
    ),
    (
        "M36 the void is routed by the approval's own branch instead of the supervisor",
        SUPERVISOR,
        '        if state.get("control_owner") == ControlOwner.SUPERVISOR.value:\n'
        '            return "supervisor"\n',
        "",
        f"{RT}::test_a_voided_action_is_re_derived_rather_than_decided",
    ),
    (
        "M37 the executor stops requiring the step's role at the point of use",
        SUPERVISOR,
        "                required_roles=ACTION_REQUIRED_ROLES,\n            )\n        except AuthorityWithdrawn as exc:",
        "                required_roles=frozenset(),\n            )\n        except AuthorityWithdrawn as exc:",
        f"{RT}::test_a_verified_requester_without_the_step_role_is_refused",
    ),
    # ---- an empty grant set is denial, not an absent restriction ---------------------
    (
        "M38 an out-of-scope GLPI entity resolves to the tenant's integration anyway",
        RESOLVER,
        "    if integration.entity_id not in context.allowed_glpi_entity_ids:\n"
        '        raise PermissionError("GLPI entity is outside the caller\'s tenant scope")',
        "    if False:\n"
        '        raise PermissionError("GLPI entity is outside the caller\'s tenant scope")',
        f"{TEST}::test_no_glpi_entity_in_scope_is_not_a_tenant_wide_integration",
    ),
    (
        "M39 an entity-bound tool is visible to a caller holding any entity at all",
        REGISTRY,
        "            and (not definition.allowed_entities or bool(definition.allowed_entities & entity_ids))",
        "            and (not definition.allowed_entities or bool(entity_ids))",
        f"{TEST}::test_a_tool_bound_to_an_entity_is_invisible_without_that_entity",
    ),
    # ---- B2: the graph side channel -------------------------------------------------
    (
        "M40 the projection drops the ACL its record declared",
        PROJECTION,
        "            extra=extra,\n"
        "            entity_ids=entity_ids,\n"
        "            group_ids=group_ids,\n"
        "            profile_ids=profile_ids,\n"
        "        )",
        "            extra=extra,\n"
        "            entity_ids=frozenset(),\n"
        "            group_ids=frozenset(),\n"
        "            profile_ids=frozenset(),\n"
        "        )",
        f"{GRAPH_TEST}::test_a_projection_derives_every_node_under_the_records_acl",
    ),
    (
        "M41 the shared visibility rule stops reading the group axis",
        GRAPH_STORE,
        "    return node.tenant_id == principal.tenant_id and principal.allows_scope(\n"
        "        entity_ids=node.entity_ids,\n"
        "        group_ids=node.group_ids,\n"
        "        profile_ids=node.profile_ids,\n"
        "    )",
        "    return node.tenant_id == principal.tenant_id and principal.allows_scope(\n"
        "        entity_ids=node.entity_ids,\n"
        "        group_ids=frozenset(),\n"
        "        profile_ids=node.profile_ids,\n"
        "    )",
        f"{GRAPH_TEST}::test_a_restricted_node_is_found_by_its_group_and_by_no_one_else",
    ),
    (
        "M42 the traversal filters the seeds but not the path",
        GRAPH_STORE,
        "        nodes = {\n"
        "            key: node\n"
        "            for key, node in self._nodes[tenant_id].items()\n"
        "            if node_is_visible(node, principal)\n"
        "        }",
        "        nodes = dict(self._nodes[tenant_id])",
        f"{GRAPH_TEST}::test_a_restricted_node_is_not_reachable_from_a_visible_one"
        f" {GRAPH_TEST}::test_a_restricted_node_cannot_be_used_as_a_seed",
    ),
    (
        "M43 the retriever stops refusing a store that cannot apply node ACLs",
        GRAPH_RETRIEVAL,
        "        if not store.supports_node_acl:\n            raise GraphAccessError(store.label)\n",
        "",
        f"{GRAPH_TEST}::test_the_retriever_refuses_a_store_that_cannot_apply_node_acls",
    ),
    (
        "M44 the side channel folds a refusal into the ordinary degradation",
        RAG_SERVICE,
        "        except GraphAccessError:\n"
        "            logger.error(\n"
        '                "Graph-RAG refused: store %r cannot apply node ACLs. No graph evidence "\n'
        '                "is served, for this query or any other.",\n'
        "                self.graph_store.label,\n"
        "            )\n"
        "            return []\n"
        "        except Exception:\n",
        "        except Exception:\n",
        f"{GRAPH_TEST}::test_the_side_channel_reports_an_unfilterable_store_rather_than_degrading",
    ),
    (
        "M45 the Cypher traversal stops filtering the far endpoint",
        NEO4J,
        "f\"AND ({_acl_where('a')}) AND ({_acl_where('b')}) \"",
        "f\"AND ({_acl_where('a')}) \"",
        f"{GRAPH_TEST}::test_both_cypher_read_paths_filter_the_whole_path",
    ),
    (
        "M46 an absent ACL array stops reading as unrestricted",
        NEO4J,
        '        f"AND (size(coalesce({alias}.group_ids, [])) = 0 "\n'
        '        f"OR any(x IN coalesce({alias}.group_ids, []) WHERE x IN $group_ids)) "\n',
        '        f"AND (size({alias}.group_ids) = 0 "\n'
        '        f"OR any(x IN {alias}.group_ids WHERE x IN $group_ids)) "\n',
        f"{GRAPH_TEST}"
        f"::test_the_cypher_predicate_covers_every_axis_and_reads_absent_as_unrestricted",
    ),
    (
        "M47 the graph read is handed the tenant instead of the whole principal",
        TOOL_GATEWAY,
        "                    group_ids=frozenset(tenant_context.allowed_glpi_group_ids),",
        "                    group_ids=frozenset(),",
        f"{SUBAGENTS}::test_the_read_gateway_carries_the_callers_group_scope_into_the_tool_call",
    ),
    # ---- B1: a refusal is not an execution ------------------------------------------
    (
        "M48 a refusal is gated on the verifier again (an outage can stop a decline)",
        API,
        '    refusal = request.decision == "rejected"',
        "    refusal = False",
        f"{TEST}::test_a_refusal_does_not_need_the_requester_reestablished",
    ),
    (
        "M49 the refusal resume runs as the approver who called it",
        RUNTIME,
        "        Command(resume=decision.model_dump(), update={}),\n"
        "        config=run_config(run, recorded),",
        "        Command(resume=decision.model_dump(), update={}),\n"
        "        config=run_config(run, context),",
        f"{TEST}::test_a_refusal_resumes_as_the_requester_without_narrowing",
    ),
    (
        "M50 the refusal path stops refusing anything but a refusal",
        RUNTIME,
        '    if decision.decision != "rejected":\n'
        '        raise ValueError("decline_run applies refusals only")',
        '    if False:\n        raise ValueError("decline_run applies refusals only")',
        f"{TEST}::test_the_refusal_path_cannot_be_borrowed_to_approve",
    ),
]

#: Pristine text of every file this script mutates, taken once before the first
#: write. A mutation experiment that dies mid-flight leaves a mutated tree behind,
#: and a mutated tree is a *silently wrong* tree: the next test run grades the
#: mutation rather than the fix. The previous version restored only in a ``finally``,
#: which a ``SIGTERM`` from a supervising timeout skips entirely -- and that is how a
#: reversal of the entitlement gate survived in ``orchestration/runtime.py``. So the
#: snapshot is taken up front and restored from an ``atexit`` hook *and* from the
#: signal handlers, which together cover every exit except ``SIGKILL``; that last
#: hole is closed by the journal ``_reconcile_interrupted_run`` reads.
PRISTINE: dict[Path, str] = {}


def _restore_all() -> None:
    for path, text in PRISTINE.items():
        if path.read_text() != text:
            path.write_text(text)
    # Without this the journal would outlive the run it describes and the next
    # start would report a crash that has already been undone.
    MANIFEST.unlink(missing_ok=True)
    LOCK.unlink(missing_ok=True)


def _acquire_lock() -> None:
    """Refuse to run while another copy of this script holds the tree.

    Two runs interleave at file granularity: each snapshots a target, writes its own
    mutation, and restores *that* snapshot -- so the second run's restore can reinstate
    the first run's mutation, or erase it, depending on which one lands last. Neither
    run can detect this from the results, because both are comparing against a file the
    other one is rewriting. The tree then ends up somewhere neither run intended, which
    is how a leftover mutation and a *silently reverted fix* both appeared at once.
    """
    if LOCK.exists():
        holder = LOCK.read_text().strip()
        if holder.isdecimal() and Path(f"/proc/{holder}").exists():
            raise SystemExit(
                f"another mutation run is in flight (pid {holder}); refusing to interleave"
            )
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    LOCK.write_text(str(os.getpid()))


def _reconcile_interrupted_run() -> None:
    """Undo a mutation that a previous run never got to revert.

    The journal is written immediately before a file is mutated and deleted
    immediately after it is restored, so its *presence* means a run died with a
    mutation in the tree. ``SIGKILL`` -- and the ``timeout(1)`` that sends it -- is
    uncatchable, so this is the only way that window is ever closed; it is what the
    earlier reversal of the entitlement gate survived through.

    The three cases are distinguished by content rather than assumed: equal to the
    pristine text means the run actually finished and only the journal is stale;
    equal to the mutated text means the crash is real and is undone here; anything
    else means the file was edited on purpose after the crash, and is left alone.
    """
    if not MANIFEST.exists():
        return
    record = json.loads(MANIFEST.read_text())
    MANIFEST.unlink()
    path, pristine, mutated = Path(ROOT, record["path"]), record["pristine"], record["mutated"]
    current = path.read_text()
    if current == pristine:
        return
    if current == mutated:
        path.write_text(pristine)
        print(
            f"NOTE: {record['path']} carried an unreverted mutation "
            f"({record['name']}); it has been restored.",
            file=sys.stderr,
        )
        return
    print(
        f"NOTE: {record['path']} changed after the interrupted run "
        f"({record['name']}); leaving it untouched.",
        file=sys.stderr,
    )


def _install_restore_handlers() -> None:
    atexit.register(_restore_all)

    def _handler(signum, _frame):  # noqa: ANN001 - signal handlers take the frame raw
        _restore_all()
        print(f"\nreceived signal {signum}; restored the working tree", file=sys.stderr)
        raise SystemExit(1 if signum != signal.SIGINT else 130)

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


targets = sorted({path for _, path, _, _, _ in MUTATIONS})
for path in targets:
    PRISTINE[path] = path.read_text()
# The anchors are checked before any handler is installed: a missing anchor is a
# stale script, not a detection result, and it must not be reported as one.
for name, path, old, _new, _tests in MUTATIONS:
    if old not in PRISTINE[path]:
        raise SystemExit(f"stale mutation: {name} -- anchor not found in {path}")
MANIFEST.parent.mkdir(parents=True, exist_ok=True)
_install_restore_handlers()
_reconcile_interrupted_run()
_acquire_lock()

failures = []
for name, path, old, new, tests in MUTATIONS:
    original = PRISTINE[path]
    mutated = original.replace(old, new, 1)
    MANIFEST.write_text(
        json.dumps(
            {
                "name": name,
                "path": str(path.relative_to(ROOT)),
                "pristine": original,
                "mutated": mutated,
            }
        )
    )
    path.write_text(mutated)
    try:
        proc = subprocess.run(
            ["uv", "run", "pytest", *tests.split(), "-q", "--no-header", "-p", "no:randomly"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=900,
        )
        tail = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()][-1]
    finally:
        path.write_text(original)
        MANIFEST.unlink(missing_ok=True)
    detected = proc.returncode != 0
    status = "RED (good)" if detected else "GREEN (BAD - no teeth)"
    print(f"{status:28} | {name}\n{'':28} | {tail}")
    if not detected:
        failures.append(name)

print()
print(
    f"{len(MUTATIONS)} mutations; "
    + ("all detected" if not failures else f"UNDETECTED: {failures}")
)
sys.exit(1 if failures else 0)
