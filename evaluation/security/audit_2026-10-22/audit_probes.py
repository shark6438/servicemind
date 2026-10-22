import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

from servicemind.evaluation.security import EvidenceKind
from servicemind.evaluation.security_grader import EvidenceOutcome
from servicemind.harness.webhooks import WebhookValidationError, verify_glpi_signature

results = {}
try:
    verify_glpi_signature(
        body=b"{}", timestamp="9" * 5000, signature="0" * 64, secret="audit-only", now=1000
    )
except Exception as exc:
    results["webhook_long_timestamp"] = {
        "exception": type(exc).__name__,
        "caught_by_endpoint": isinstance(exc, WebhookValidationError),
        "int_max_str_digits": sys.get_int_max_str_digits(),
    }
spec = importlib.util.spec_from_file_location(
    "security_driver", Path("scripts/verify_phase7_security.py")
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
results["mixed_pass_skip"] = module._aggregate(
    [("PASSED", EvidenceOutcome.PASSED), ("SKIPPED", EvidenceOutcome.UNEXERCISED)]
).value
module.run_tests = lambda *args, **kwargs: {}
module.acceptance_verdicts = lambda: ({"ACC-AUDIT": "BLOCKED"}, "")
scenario_set = SimpleNamespace(
    scenarios=[
        SimpleNamespace(
            id="SEC-AUDIT",
            evidence=[
                SimpleNamespace(kind=EvidenceKind.ACCEPTANCE, ref="ACC-AUDIT", mutation=None)
            ],
        )
    ]
)
try:
    module.collect_evidence(scenario_set, only=set(), with_mutations=False)
except Exception as exc:
    results["blocked_acceptance_import"] = {"exception": type(exc).__name__, "message": str(exc)}
Path("audit-probes.json").write_text(json.dumps(results, indent=2) + "\n")
print(json.dumps(results, indent=2))
