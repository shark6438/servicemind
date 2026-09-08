---
{"skill_id":"incident-triage","version":"1.0.0","checksum":"a86eddf379a1496bdbb8bd475b3a56141790924c8b74f1cde7e2a0073d2c9005","scope":"global","tenant_id":null,"allowed_agents":["analysis","reviewer"],"required_tools":[],"risk_level":"medium","input_schema":{"type":"object","required":["ticket","evidence"]},"output_schema":{"type":"object","required":["classification","priority","assignment","citations"]},"evidence_requirements":["Current ticket facts","Impact and urgency evidence","Candidate group evidence"],"context_requirements":["task","verified evidence","priority policy"],"tests":["Priority is derived from impact and urgency","Every recommendation has a citation","Insufficient evidence causes abstention"],"keywords":["incident","ticket","triage","故障","工单","分诊"],"created_at":"2026-09-08T00:00:00Z","deprecated_at":null}
---
# Incident triage

Classify the incident from verified evidence. Derive priority from recorded impact and urgency. Recommend only groups present in current evidence. Cite each material claim and abstain when the evidence cannot support it. Any mutation remains subject to Reviewer and approval policy.
