import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
BATCH = Path(__file__).resolve().parent
local = json.loads((ROOT / "evaluation/security/scenarios.local.v1.json").read_text())
by_id = {case["id"]: case for case in local["scenarios"]}
links = [
    ["SEC-CRED-01", "LOCAL-RUN-SCOPE"],
    ["SEC-INJECT-02", "SEC-APPROVAL-04", "SEC-WRITE-03"],
    ["SEC-INJECT-03", "SEC-INJECT-04", "SEC-TENANT-02"],
    ["SEC-SELF-02", "SEC-CRED-01", "SEC-WRITE-04"],
    ["SEC-WRITE-03", "SEC-APPROVAL-06"],
    ["SEC-APPROVAL-04", "SEC-WRITE-01", "SEC-WRITE-04"],
    ["SEC-IDEM-01", "SEC-WRITE-06"],
    ["SEC-AUTHZ-04", "SEC-AUTHZ-05", "SEC-CRED-02"],
    ["LOCAL-JWT", "SEC-BOUNDS-03", "SEC-AUTHZ-06"],
    ["LOCAL-RUN-ROLE", "LOCAL-RUN-SCOPE"],
    ["SEC-NARROW-03", "SEC-NARROW-05", "SEC-NARROW-07", "SEC-NARROW-08"],
    ["LOCAL-RUN-SCOPE", "SEC-TENANT-06"],
    ["SEC-CRED-02"],
    ["SEC-AUTHZ-04"],
    ["SEC-WRITE-04", "SEC-CRED-01"],
    ["LOCAL-GRADING", "LOCAL-PROVENANCE"],
    ["LOCAL-GENERIC"],
    ["SEC-WRITE-05"],
    ["SEC-CRED-01"],
    ["LOCAL-WEBHOOK-RATE", "LOCAL-WEBHOOK-BOUNDS", "SEC-BOUNDS-01"],
    ["SEC-INJECT-01", "SEC-INJECT-02", "SEC-INJECT-04"],
    ["LOCAL-MEMORY-REVIEW"],
    ["SEC-INJECT-03", "SEC-IDEM-02", "SEC-TENANT-02"],
    ["SEC-TENANT-04", "SEC-TENANT-02", "SEC-INJECT-04"],
    ["SEC-NARROW-01", "SEC-NARROW-03", "SEC-AUTHZ-01"],
    ["SEC-TENANT-01", "SEC-SELF-02", "SEC-CRED-01"],
    ["SEC-TENANT-03", "SEC-IDEM-03"],
    ["SEC-CRED-01", "SEC-CRED-02"],
    ["SEC-LIMIT-01", "SEC-LIMIT-02", "SEC-LIMIT-03", "SEC-LIMIT-04", "SEC-LIMIT-05"],
    ["SEC-WRITE-04", "SEC-IDEM-01", "SEC-WRITE-06"],
    ["SEC-OUTBOX-01", "SEC-OUTBOX-02", "SEC-OUTBOX-03", "SEC-TENANT-06"],
    ["SEC-VERIFIER-02", "SEC-NARROW-04", "SEC-WRITE-02", "SEC-CRASH-01"],
    ["SEC-WRITE-03", "SEC-APPROVAL-02"],
    ["LOCAL-RUN-ROLE"],
    ["SEC-WRITE-04", "SEC-WRITE-06"],
    ["SEC-APPROVAL-04", "SEC-APPROVAL-06", "SEC-NARROW-01"],
    ["LOCAL-CANCEL"],
    ["LOCAL-CANCEL", "SEC-CRASH-01"],
    ["SEC-LIMIT-06", "SEC-CRASH-03", "SEC-CRASH-04", "SEC-CRASH-05"],
    ["SEC-WEBHOOK-01", "SEC-WEBHOOK-02", "SEC-WEBHOOK-03", "LOCAL-WEBHOOK-BOUNDS"],
    ["SEC-CRED-02", "SEC-TENANT-02", "SEC-SELF-03"],
    ["LOCAL-CIPHER"],
    ["SEC-OUTBOX-02", "SEC-IDEM-03"],
    ["SEC-WEBHOOK-01", "SEC-WEBHOOK-03", "SEC-OUTBOX-01"],
    ["LOCAL-GRADING"],
    ["LOCAL-PROVENANCE", "LOCAL-GRADING"],
    ["LOCAL-GRADING", "SEC-WRITE-04"],
    ["LOCAL-RUN-SCOPE", "LOCAL-CANCEL", "SEC-NARROW-03"],
]
extra = {
    1: ["test_nonlexical_injections_cannot_replace_a_durable_approval"],
    2: ["test_nonlexical_injections_cannot_replace_a_durable_approval"],
    3: ["test_nonlexical_injections_cannot_replace_a_durable_approval"],
    15: ["test_mcp_real_network_redirect_never_forwards_credentials_and_valid_peer_works"],
    18: ["test_attachments_refuse_symlink_escape_and_do_not_execute_code"],
    19: ["test_mcp_real_network_redirect_never_forwards_credentials_and_valid_peer_works"],
    20: ["test_large_headers_are_rejected_before_auth_and_normal_health_works"],
    28: ["test_mcp_real_network_redirect_never_forwards_credentials_and_valid_peer_works"],
    42: [
        "test_rotation_preserves_timestamp_and_new_key_is_sufficient",
        "test_rotation_is_atomic_tenant_scoped_and_dry_run_does_not_write",
    ],
    46: ["test_serving_build_identity_is_captured_once_and_excludes_secrets"],
    48: ["test_nonlexical_injections_cannot_replace_a_durable_approval"],
}
artifacts = {
    13: [
        "supply-chain/python-runtime.json",
        "supply-chain/frontend-final.json",
        "supply-chain/llama-final-image.json",
        "supply-chain/llama-final-sbom.cdx.json",
        "supply-chain/audit-gate-release.log",
        "supply-chain/python-runtime-sbom.cdx.json",
        "supply-chain/frontend-sbom.cdx.json",
    ],
    16: ["supply-chain/audit-gate-release.log"],
    20: ["network-probe.json"],
    34: ["browser-deployed-fixed.log"],
    43: ["retention-observation.json"],
    44: ["webhook-live.json"],
    48: ["mutation-summary.json", "holdout-observation.json"],
}
live_cases = {
    5: ["ACC-09a", "ACC-11"],
    6: ["ACC-11"],
    10: ["ACC-04c"],
    11: ["ACC-18", "ACC-19", "ACC-20", "ACC-21", "ACC-23"],
    30: ["ACC-11"],
    32: ["ACC-10a", "ACC-22"],
    33: ["ACC-09a"],
    35: ["ACC-11"],
    36: ["ACC-09b", "ACC-11"],
}
additional = {
    7: [
        "test_phase6_tool_platform.py::test_gateway_enforces_contract_policy_audit_and_exact_replay"
    ],
    8: [
        "test_phase6_tool_platform.py::test_gateway_rejects_schema_capability_role_entity_taint_and_secret"
    ],
    14: [
        "test_phase6_tool_platform.py::test_registry_versions_are_immutable_and_visibility_is_scoped"
    ],
    15: [
        "test_phase6_tool_platform.py::test_mcp_rejects_session_header_and_wrong_issuer",
        "test_phase6_tool_platform.py::test_native_and_stateless_mcp_provider_contract_parity",
    ],
    18: [
        "test_phase6_tool_platform.py::test_mcp_resources_are_strict_and_direct_update_is_not_exposed"
    ],
    23: [
        "test_phase4_index_lifecycle_live.py::test_a_suspended_row_is_unreachable_through_a_real_filtered_search",
        "test_phase5_governance.py::test_revocation_cascades_through_supporting_episodes",
        "test_phase5_governance.py::test_active_procedure_is_revoked_when_all_supporting_episodes_expire",
    ],
    24: [
        "test_phase5_governance.py::test_semantic_cache_is_tenant_partitioned",
        "test_phase5_governance.py::test_a_memory_reaches_the_model_with_the_identity_of_who_wrote_it",
    ],
    26: [
        "test_security_completion.py::test_subagent_results_are_bound_to_the_invocation",
        "test_supervisor_runtime.py::test_a_foreign_task_result_never_reaches_joined_evidence",
    ],
    44: [
        "test_security_completion.py::test_private_webhook_proxy_has_one_bounded_route_and_never_forwards_credentials",
        "test_security_completion.py::test_glpi_cra_challenge_uses_the_routed_tenant_without_starting_a_run",
        "test_security_completion.py::test_proxy_cra_is_scoped_to_one_uuid_path_and_one_bounded_query",
    ],
}
additional.setdefault(37, []).append(
    "test_security_remediation.py::test_real_database_cancel_http_materializes_updated_at"
)
additional.setdefault(38, []).append(
    "test_security_remediation.py::test_real_database_cancel_http_materializes_updated_at"
)
rows = []
text = (ROOT / "docs/SECURITY_ASSESSMENT_EXECUTION_PLAN_2026-10-22.md").read_text()
specs = re.findall(r"^\| (SM-R\d{2}-\d{2}) \| (.*?) \| (.*?) \|$", text, re.M)
assert len(specs) == len(links) == 48
for index, ((case_id, attack, expected), references) in enumerate(
    zip(specs, links, strict=True), 1
):
    selectors = sorted(
        {
            item["ref"]
            for ref in references
            for item in by_id[ref]["evidence"]
            if item["kind"] == "test"
        }
    )
    selectors += [
        f"tests/servicemind/test_security_completion.py::{name}" for name in extra.get(index, [])
    ]
    selectors += ["tests/servicemind/" + item for item in additional.get(index, [])]
    rows.append(
        {
            "id": case_id,
            "attack_specification": attack,
            "expected_result_and_positive_control": expected,
            "control_scenarios": references,
            "test_selectors": selectors,
            "required_live_cases": live_cases.get(index, []),
            "required_artifacts": artifacts.get(index, []),
            "evidence_mode": "deterministic controls + synthetic model proposals + real storage/network/browser + local open model live workflow",
            "verdict": "PENDING",
        }
    )
(BATCH / "case-matrix.json").write_text(
    json.dumps(
        {"schema": "servicemind-security-48-v1", "cases": rows}, ensure_ascii=False, indent=2
    )
    + "\n"
)
print(
    json.dumps(
        {
            "specifications": len(rows),
            "unique_test_selectors": len({s for r in rows for s in r["test_selectors"]}),
        }
    )
)
