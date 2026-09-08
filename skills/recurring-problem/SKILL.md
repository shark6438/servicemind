---
{"skill_id":"recurring-problem","version":"1.0.0","checksum":"23aab8132aa6814f9f0da83afcfd8139ba7840d61e2b6096d5fab88aca508b36","scope":"global","tenant_id":null,"allowed_agents":["analysis","reviewer"],"required_tools":[],"risk_level":"medium","input_schema":{"type":"object","required":["incident_history","evidence"]},"output_schema":{"type":"object","required":["pattern","confidence","citations"]},"evidence_requirements":["Two or more comparable incidents","Verified final outcomes","Time-bounded similarity evidence"],"context_requirements":["task","episodic memory","verified evidence"],"tests":["A single incident cannot establish recurrence","Conflicting outcomes are disclosed","Problem creation remains a reviewed proposal"],"keywords":["recurring","repeat","problem","trend","重复","复发","问题"],"created_at":"2026-09-08T00:00:00Z","deprecated_at":null}
---
# Recurring problem analysis

Identify a recurring pattern only when at least two comparable, verified incidents support it. Report conflicting outcomes and the observation window. Treat historical episodes as experience rather than an official procedure. Creating a Problem record requires the normal review and approval path.
