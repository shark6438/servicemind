---
{"skill_id":"change-risk","version":"1.0.0","checksum":"151adf755b1a125bc14927a6e491bd7c914f14519e526f1ce75a1ce5a2930c50","scope":"global","tenant_id":null,"allowed_agents":["analysis","reviewer"],"required_tools":[],"risk_level":"high","input_schema":{"type":"object","required":["change","affected_services","evidence"]},"output_schema":{"type":"object","required":["risk","blast_radius","controls","citations"]},"evidence_requirements":["Change scope","Affected service or CI evidence","Rollback evidence"],"context_requirements":["task","change policy","verified evidence"],"tests":["Missing rollback plan escalates risk","High blast radius requires human review","Memory cannot lower policy risk"],"keywords":["change","release","rollback","risk","变更","发布","回滚","风险"],"created_at":"2026-09-08T00:00:00Z","deprecated_at":null}
---
# Change risk review

Assess blast radius, service criticality, timing, dependency and rollback readiness from verified evidence. Missing rollback evidence increases risk. High-impact or unclear changes require human review. A learned memory may add context but cannot lower the policy-defined risk or approval requirement.
