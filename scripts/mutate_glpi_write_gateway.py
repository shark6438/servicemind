"""Revert each decision that put the GLPI write behind the tool gateway, and require the tests to notice.

The defect this locks was architectural rather than functional: the harness performed the
platform's one side effect by reaching ``GlpiClient`` itself, so the call that most needed
a policy decision and an invocation row was the only call with neither. Restoring the
gateway is one line; making it *mean* something is the rest of this file, because a
gateway that takes the caller's word for what was approved is a pass-through with extra
steps.

Every mutation below therefore removes one of the things that make the routing real --
the audit record, the durable approval lookup, the equality between the bytes and the
approved intent, the marker derivation, the fresh read-back. Each must be caught, and
the ones that were added after the first pass are the ones an implementation would
naturally omit: a provider that trusts ``arguments["content"]`` because "the harness
built it", or a marker taken from the caller because "it is only bookkeeping".
"""

import pathlib
from pathlib import Path

from mutation_harness import Mutation, run_mutations

ROOT = pathlib.Path(__file__).resolve().parent.parent
EXPERIMENT = "glpi_write_gateway"
EXECUTOR = ROOT / "src/servicemind/harness/executor.py"
PROVIDERS = ROOT / "src/servicemind/tool_platform/providers.py"
GATEWAY = ROOT / "src/servicemind/tool_platform/gateway.py"

TEST = "tests/servicemind/test_enterprise_subagents.py"


def _test(name: str) -> str:
    return f"{TEST}::{name}"


MUTATIONS = [
    (
        "M01 the gateway stops recording the policy decision",
        GATEWAY,
        "        decision = await self.policy.decide(definition, call)\n"
        "        await self.audit.record_policy(definition, call, decision)\n",
        "        decision = await self.policy.decide(definition, call)\n",
        _test("test_the_write_leaves_a_policy_decision_and_an_invocation_behind"),
    ),
    (
        "M02 the harness stops citing the intent it acts on",
        EXECUTOR,
        '            approval_ref=f"action-intent://{intent.id}",\n',
        "            approval_ref=None,\n",
        _test("test_a_write_whose_stored_body_matches_is_verified"),
    ),
    (
        "M03 the harness declares no capability for the tool it is calling",
        EXECUTOR,
        "            capabilities=frozenset({APPEND_FOLLOWUP_TOOL}),\n",
        "            capabilities=frozenset(),\n",
        _test("test_a_write_whose_stored_body_matches_is_verified"),
    ),
    (
        "M04 the approval is bound to nothing, so any call shape satisfies it",
        EXECUTOR,
        '        return call.model_copy(update={"approval_binding": call.approval_digest})\n',
        "        return call.model_copy(update={\"approval_binding\": '0' * 64})\n",
        _test("test_a_write_whose_stored_body_matches_is_verified"),
    ),
    (
        "M05 a failed call leaves the action looking untried",
        EXECUTOR,
        "            except Exception:\n"
        "                # Every way this can fail -- the policy refusing, the provider failing, the\n"
        "                # read-back disagreeing -- leaves the action attempted and unproven, which\n"
        "                # is what FAILED means. Recording it here rather than at each raise keeps\n"
        "                # the status in step with the exception however the gateway grows.\n"
        "                await repository.update_action_status(intent.id, ActionStatus.FAILED)\n"
        "                raise\n",
        "            except Exception:\n                raise\n",
        _test("test_a_principal_without_the_action_role_cannot_write"),
    ),
    (
        "M06 a reconciled duplicate is audited as a second creation",
        EXECUTOR,
        "            if duplicate_suppressed:\n"
        "                # A reconciled duplicate is a write that already happened; auditing it as\n"
        "                # a new followup would put two creation events in the ledger for one row.\n"
        "                return result\n",
        "",
        _test("test_a_crash_recovered_duplicate_with_the_approved_body_is_suppressed"),
    ),
    (
        "M07 the provider writes whatever body the caller sent",
        PROVIDERS,
        '        if content != str(approved.get("content", "")):\n'
        '            raise PermissionError("the content being written is not the content that was approved")\n',
        "",
        _test("test_the_gateway_will_not_write_a_body_the_approval_does_not_cover"),
    ),
    (
        "M08 the provider trusts a marker the caller chose",
        PROVIDERS,
        "        if marker != self_authored_marker(call.run_id, stored.action_hash):\n"
        '            raise PermissionError("the idempotency marker does not name the approved action")\n',
        "",
        _test("test_a_marker_that_does_not_name_the_approved_action_is_refused"),
    ),
    (
        "M09 the reconcile branch stops comparing the body it found",
        PROVIDERS,
        "                if not body_matches(duplicate.content, expected_text):\n"
        "                    raise RuntimeError(\n"
        '                        "GLPI already holds this run\'s followup, and its body is not the "\n'
        '                        "approved content: the persisted effect differs from the intent, so "\n'
        '                        "it is neither this write nor a safe duplicate"\n'
        "                    )\n",
        "",
        _test("test_a_crash_recovered_duplicate_with_a_different_body_is_refused"),
    ),
    (
        "M10 the read-back trusts the write instead of asking GLPI again",
        PROVIDERS,
        "        config = await resolve_glpi_config(self._context(call))\n"
        "        async with GlpiClient(config) as client:\n"
        '            rows = await client.list_ticket_followups(int(arguments["ticket_id"]))\n'
        '        stored = next((item for item in rows if item.id == output.get("followup_id")), None)\n'
        "        if stored is None:\n"
        "            return False\n"
        "        if not body_matches(stored.content, expected_text):\n"
        "            raise ToolVerificationFailed(\n"
        '                "GLPI read-after-write verification failed: the stored followup is not "\n'
        '                "the approved content"\n'
        "            )\n"
        "        return True\n",
        "        del arguments, output, call\n        return True\n",
        _test("test_a_write_whose_stored_body_was_truncated_is_not_verified"),
    ),
    (
        "M11 the read-back finds the row but stops comparing the text",
        PROVIDERS,
        "        if not body_matches(stored.content, expected_text):\n"
        "            raise ToolVerificationFailed(\n"
        '                "GLPI read-after-write verification failed: the stored followup is not "\n'
        '                "the approved content"\n'
        "            )\n        return True\n",
        "        return True\n",
        _test("test_a_write_whose_stored_body_was_truncated_is_not_verified"),
    ),
    (
        "M12 a missing row counts as a verified write",
        PROVIDERS,
        "        if stored is None:\n            return False\n",
        "        if stored is None:\n            return True\n",
        _test("test_a_write_glpi_does_not_have_is_not_verified"),
    ),
    (
        "M13 only an unclaimed approval authorises the write the executor is performing",
        PROVIDERS,
        "_APPROVAL_STANDS = frozenset({ActionStatus.APPROVED.value, ActionStatus.EXECUTING.value})\n",
        "_APPROVAL_STANDS = frozenset({ActionStatus.APPROVED.value})\n",
        _test("test_the_write_proceeds_while_the_executor_holds_the_claim"),
    ),
    (
        "M14 the status is never consulted, so any cited intent authorises a write",
        PROVIDERS,
        "        if stored.status not in _APPROVAL_STANDS:\n"
        '            raise PermissionError("the cited ActionIntent is not approved")\n'
        "        return stored\n",
        "        return stored\n",
        _test("test_a_call_citing_an_intent_that_is_not_approved_is_refused"),
    ),
    (
        "M15 a terminal status is read as a standing approval",
        PROVIDERS,
        "_APPROVAL_STANDS = frozenset({ActionStatus.APPROVED.value, ActionStatus.EXECUTING.value})\n",
        "_APPROVAL_STANDS = frozenset(\n"
        "    {ActionStatus.APPROVED.value, ActionStatus.EXECUTING.value, ActionStatus.SUCCEEDED.value}\n"
        ")\n",
        _test("test_a_call_citing_an_intent_that_is_not_approved_is_refused"),
    ),
]


if __name__ == "__main__":
    raise SystemExit(
        run_mutations(
            root=ROOT,
            experiment=EXPERIMENT,
            mutations=[
                Mutation(name=name, path=Path(path), old=old, new=new, tests=(tests,))
                for name, path, old, new, tests in MUTATIONS
            ],
        )
    )
