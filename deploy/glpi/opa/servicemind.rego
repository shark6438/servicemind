package servicemind.tool

import rego.v1

default decision := {
    "allow": false,
    "requires_approval": false,
    "allowed_fields": [],
    "reason_codes": ["OPA_DEFAULT_DENY"],
    "policy_version": "servicemind-tool-policy-v1",
}

role_allowed if {
    some role in input.identity.roles
    role in input.tool.allowed_roles
}

entity_allowed if {
    count(input.tool.allowed_entities) == 0
}

entity_allowed if {
    some entity in input.entity_ids
    entity in input.tool.allowed_entities
}

approval_allowed if {
    not input.tool.requires_approval
}

approval_allowed if {
    input.tool.requires_approval
    input.approval_ref != null
}

decision := {
    "allow": true,
    "requires_approval": input.tool.requires_approval,
    "allowed_fields": object.keys(input.tool.input_schema.properties),
    "redactions": [],
    "reason_codes": ["OPA_POLICY_ALLOWED"],
    "policy_version": "servicemind-tool-policy-v1",
} if {
    input.tool.active
    role_allowed
    entity_allowed
    approval_allowed
    count(input.taint_labels) == 0
}
