import ast
from pathlib import Path

#: The raw GLPI write. Exactly one module in the tree may perform it, and the site is
#: named here rather than inferred from an absence so that moving it is a decision
#: somebody makes instead of a diff nobody reads.
#:
#: It used to live in ``harness/executor.py``, and that is why the platform's one side
#: effect was also its one unaudited call: the harness reached ``GlpiClient`` itself, so
#: no policy decision was taken and no invocation row was written. The write now sits in
#: the tool provider, where the gateway decides whether it may happen and records that it
#: did. The harness keeps what is genuinely its own -- the persisted-intent equality, the
#: approval status, the durable idempotency claim -- and builds a call the gateway can
#: refuse.
GLPI_WRITE_SITE = "src/servicemind/tool_platform/providers.py"


def test_the_raw_glpi_write_exists_in_exactly_one_module() -> None:
    """The single write site, asserted as a set.

    ``offenders == []`` would also be satisfied by the call disappearing from the tree
    altogether -- a green test over a platform that no longer writes anything. Equality
    against the one allowed module cannot pass that way, and it fails the moment a second
    module reaches for the write, which is the property that matters.
    """
    root = Path("src/servicemind")
    sites: list[str] = []
    for path in root.rglob("*.py"):
        if path.as_posix().endswith("integrations/glpi/client.py"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            if isinstance(function, ast.Attribute) and function.attr == "append_ticket_followup":
                sites.append(path.as_posix())
    assert sorted(set(sites)) == [GLPI_WRITE_SITE]


def test_the_harness_reaches_glpi_only_through_the_gateway() -> None:
    """The other half of the same property, stated where it is easy to regress.

    A harness that can still name ``GlpiClient`` can still write without a gateway call,
    whether or not it currently does -- and re-adding the line that does is one line. This
    asks about imports rather than about the text, because the harness should be free to
    *explain* in a comment why it no longer reaches for the client, and a grep-shaped
    assertion would punish the explanation instead of the capability.
    """
    tree = ast.parse(Path("src/servicemind/harness/executor.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    assert imported & {"GlpiClient", "resolve_glpi_config"} == set()
    assert "build_tool_gateway" in imported


def test_only_phase2_harness_imports_glpi_write_method() -> None:
    action_source = Path("src/servicemind/agents/action.py").read_text(encoding="utf-8")
    reviewer_source = Path("src/servicemind/agents/reviewer.py").read_text(encoding="utf-8")
    analysis_source = Path("src/servicemind/agents/analysis.py").read_text(encoding="utf-8")
    assert "GlpiClient" not in action_source
    assert "GlpiClient" not in reviewer_source
    assert "GlpiClient" not in analysis_source
    assert "CredentialCipher" not in action_source
