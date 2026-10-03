"""Measure the *third* gap between the committed numbers and the deployed pipeline.

``scripts/measure_phase4_production_query_arms.py`` closes two of the three differences
between the TechQA harness and production: the harness runs ``use_query_model=False`` and
``use_rewrites=False``, while ``agents/knowledge.py`` runs the model and the multi-query
fan-out. Both are now measured.

The third difference is the *input to the rewrite stage*, and it is not a flag:

    src/servicemind/orchestration/supervisor_workflow.py:1000
        kwargs["model_query"] = json.dumps(context_envelope.model_payload(), ...)

``QueryProcessor.process`` sends ``model_query or query`` to the model
(``rag/query.py``), so wherever the platform assembles a context envelope the model is
asked to rewrite a JSON array of context items -- not the user's question. The question
survives inside it, as ``items[0].content = {"goal": ..., "ticket_id": ...}``
(``phase5_governance.py:348``), which is a very different prompt input from the bare
question. ``SERVICEMIND_CONTEXT_ENABLED`` defaults to *False* but the deployment sets it
*True* (``deploy/glpi/.env.example:159`` / ``.env``), so the deployed pipeline takes this
branch.

This probe reproduces that branch through the platform's own code -- the real
``Phase5Governance.build_fast_knowledge_context``, the real ``ContextBuilder``, the real
``ContextEnvelope.model_payload``, and a ``NullContextArtifactSink`` so nothing is
written -- and then measures the arms that result. Nothing here is reconstructed by hand:
if the envelope's shape changes, this probe measures the new shape.

What it does not do: the *knowledge-task* path (``build_context``) assembles memory and
ticket items that the release set does not have, so the fast-path envelope is the one that
can be reproduced off-line. The difference between the two is which items are in the list,
not whether the rewrite sees JSON instead of a question.

The index is not rebuilt. ``--index-prefix`` names the kept index that
``measure_phase4_production_query_arms.py`` left behind, so this probe costs two arm passes
and no ingest.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import selectors
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from servicemind.context.repository import NullContextArtifactSink
from servicemind.rag.models import TeiEmbeddingProvider, TeiReranker
from servicemind.rag.opensearch import OpenSearchKnowledgeIndex
from servicemind.rag.query import QueryProcessor
from servicemind.rag.service import EnterpriseRAG, build_opensearch_client

ROOT = Path(__file__).resolve().parents[1]
FUNNEL_SCRIPT = ROOT / "scripts/measure_phase4_production_query_arms.py"
PLAIN_CAPTURE = ROOT / "evaluation/gold/techqa_rewrites.v1.json"
ENVELOPE_CAPTURE = ROOT / "evaluation/gold/techqa_rewrites_envelope.v1.json"
#: The same stage re-run on the *same* input. A difference between the envelope pass and
#: the bare-question pass is only evidence about the envelope if re-running the bare
#: question alone reproduces itself; the rewrite stage is a sampled model, so without
#: this control the sensitivity number would be mostly noise.
REPEAT_CAPTURE = ROOT / "evaluation/gold/techqa_rewrites_repeat.v1.json"
SELECTION = ROOT / "evaluation/gold/phase4_proxy_release_v1.2.json"
DATA = ROOT / "data/phase4/raw/eval/techqa-rag-eval"
FUNNEL_REPORT = ROOT / "evaluation/reports/phase4_query_arm_funnel_latest.json"
#: This file, as an input to its own report -- see the funnel script for why.
PRODUCER = Path(__file__).resolve()
OUT_JSON = ROOT / "evaluation/reports/phase4_envelope_query_arm_latest.json"
OUT_MD = ROOT / "evaluation/reports/phase4_envelope_query_arm_latest.md"

TENANT = UUID("11111111-1111-4111-8111-111111111111")
#: The fast path's envelope carries a ticket id in its task item. The release set has no
#: GLPI tickets, so the value is a constant; it is also not what the model rewrites -- the
#: goal is -- so a real ticket number would change the prompt only by a few digits.
TICKET = "TECHQA-RELEASE"
RUN_ID = UUID("11111111-1111-4111-8111-1111111111ff")

ARMS = (
    {
        "name": "c0_env",
        "use_query_model": True,
        "use_rewrites": False,
        "candidate_k": None,
        "reading": "production rewrite input (context envelope), fan-out off",
    },
    {
        "name": "c0_mq_env",
        "use_query_model": True,
        "use_rewrites": True,
        "candidate_k": None,
        "reading": "the deployed arm as closely as it can be reproduced off-line: envelope "
        "input, production normalisation, multi-query fan-out",
    },
    {
        "name": "c0_mq",
        "use_query_model": True,
        "use_rewrites": True,
        "candidate_k": None,
        "reading": "in-process control: the same fan-out arm on the bare-question capture, "
        "re-measured here so both rows share one index generation and one process",
    },
)
#: Which capture each arm replays.
CAPTURE_OF = {"c0_env": "envelope", "c0_mq_env": "envelope", "c0_mq": "plain"}


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def envelope_for(query: str) -> str:
    """The deployed ``model_query`` for one question, built by the platform's own code."""
    from servicemind.orchestration.phase5_governance import Phase5Governance

    governance = Phase5Governance(context_sink=NullContextArtifactSink())
    envelope = await governance.build_fast_knowledge_context(
        state={
            "tenant_id": str(TENANT),
            "run_id": str(RUN_ID),
            "goal": query,
            "ticket_id": TICKET,
            "allowed_glpi_entity_ids": [1],
            "group_ids": [],
            "profile_ids": [],
        }
    )
    if envelope is None:
        raise RuntimeError(
            "the platform returned no envelope: SERVICEMIND_CONTEXT_ENABLED is off, and "
            "this probe would then be measuring the bare-question arm under a new name"
        )
    return json.dumps(envelope.model_payload(), ensure_ascii=False)


def _model_output(entry: dict[str, Any]) -> tuple[str | None, tuple[str, ...]]:
    """What the model returned for one query: its normalization and its paraphrases.

    The comparison below is a claim about the model, so it reads the model's own fields.
    It used to read ``normalized_query``, which was the model's normalization only while
    the processor overwrote the searched text with it; now that the user's question is the
    anchor that field is the same string in every capture and would report a sensitivity
    of zero for any input whatsoever.
    """
    normalization = entry.get("model_normalized_query")
    rewrites = entry.get("rewritten_queries") or []
    return normalization, tuple(rewrite.casefold() for rewrite in rewrites)


def _comparison(plain: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
    """How far the envelope input moves the model's output -- what the arms search with."""
    shared = sorted(set(plain["queries"]) & set(envelope["queries"]))
    changed: list[str] = []
    rewrites_only: list[str] = []
    without_answer: list[str] = []
    examples: list[dict[str, str]] = []
    for query_id in shared:
        left = plain["queries"][query_id]
        right = envelope["queries"][query_id]
        left_norm, left_rewrites = _model_output(left)
        right_norm, right_rewrites = _model_output(right)
        if left_norm is None or right_norm is None:
            # A capture entry the model never answered is the deterministic fallback. It is
            # not an observation of the model on either input, so a difference between it
            # and a real answer is a swallowed failure showing through, not an envelope
            # effect. Counted and excluded rather than compared.
            without_answer.append(query_id)
            continue
        same_norm = (
            " ".join(left_norm.split()).casefold() == " ".join(right_norm.split()).casefold()
        )
        if not same_norm:
            changed.append(query_id)
            if len(examples) < 3:
                examples.append(
                    {
                        "query": left["query"],
                        "bare_question_normalised": left_norm,
                        "envelope_normalised": right_norm,
                    }
                )
        elif left_rewrites != right_rewrites:
            rewrites_only.append(query_id)
    compared = len(shared) - len(without_answer)
    return {
        "queries_compared": compared,
        "queries_without_a_model_answer": len(without_answer),
        "model_output_changed": len(changed),
        "model_output_changed_share": round(len(changed) / compared, 4) if compared else 0.0,
        "model_normalisation_changed": len(changed),
        "only_the_rewrites_changed": len(rewrites_only),
        "unchanged": compared - len(changed) - len(rewrites_only),
        "examples": examples,
        "reading": (
            "the model is asked to rewrite a JSON envelope instead of the question. This "
            "counts how often that changes the model's output -- the normalization and the "
            "paraphrases the lexical arms search with -- not whether the answer is better. "
            "The dense anchor is the user's own question under both inputs, so no envelope "
            "can move it, and a sensitivity of zero here would mean the input never reached "
            "the model at all."
        ),
    }


def _capture_is_reusable(cached: dict[str, Any], expected_ids: set[str]) -> bool:
    """Whether a committed capture may stand in for a fresh one.

    Same rule as the funnel's own capture: a sample that cannot say whether the model
    answered is not a sample of the model, and the comparison this probe publishes is a
    claim *about the model*. Re-using an unflagged capture would publish a swallowed
    fallback as the stage's own output; re-using one from another query set would
    publish numbers measured on different questions.
    """
    queries = cached.get("queries", {})
    if not all(
        isinstance(entry, dict) and "provenance" in entry and "model_normalized_query" in entry
        for entry in queries.values()
    ):
        return False
    return set(queries) == expected_ids


async def capture(
    *,
    concurrency: int,
    force: bool,
    limit: int | None = None,
    mode: str = "envelope",
) -> dict[str, Any]:
    funnel = _load(FUNNEL_SCRIPT, "phase4_query_arms")
    release = funnel._load_release_module()
    gold = release._gold_set(
        json.loads(SELECTION.read_text(encoding="utf-8")),
        json.loads((DATA / "train.json").read_text(encoding="utf-8")),
    )
    target = ENVELOPE_CAPTURE if mode == "envelope" else REPEAT_CAPTURE
    queries = gold.queries if limit is None else gold.queries[:limit]
    if target.exists() and not force and limit is None:
        cached = json.loads(target.read_text(encoding="utf-8"))
        if _capture_is_reusable(cached, {query.id for query in gold.queries}):
            print(f"{mode} capture reused: {target}")
            return cached
        print(f"{mode} capture is unusable for this run (unflagged or stale); recapturing")

    processor = QueryProcessor()
    semaphore = asyncio.Semaphore(concurrency)

    async def one(query_id: str, text: str) -> tuple[str, dict[str, Any]]:
        async with semaphore:
            # Built once and passed on: the envelope is what the model is asked to
            # rewrite, and hashing a different object than the one sent would make the
            # cache look like evidence about a prompt that never ran.
            model_query = await envelope_for(text) if mode == "envelope" else None
            processed = await processor.process(text, use_model=True, model_query=model_query)
            return query_id, {
                "query": text,
                "model_query_sha256": hashlib.sha256(
                    (model_query if model_query is not None else text).encode("utf-8")
                ).hexdigest(),
                "normalized_query": processed.normalized_query,
                "model_normalized_query": processed.model_normalized_query,
                "rewritten_queries": list(processed.rewritten_queries),
                "identifiers": list(processed.identifiers),
                "entities": list(processed.entities),
                "intent": processed.intent.value,
                "language": processed.language,
                "provenance": processed.provenance.value,
            }

    started = time.perf_counter()
    results = await asyncio.gather(
        *(one(query.id, query.query) for query in queries), return_exceptions=True
    )
    captured: dict[str, dict[str, Any]] = {}
    failures: list[str] = []
    for query, value in zip(queries, results, strict=True):
        if isinstance(value, BaseException):
            failures.append(f"{query.id}: {type(value).__name__}: {value}")
            continue
        captured[value[0]] = value[1]
    if failures:
        raise RuntimeError(f"{len(failures)} queries failed to capture: {failures[:5]}")

    from core import settings

    payload = {
        "schema_version": (
            "phase4-techqa-rewrites-envelope-v1"
            if mode == "envelope"
            else "phase4-techqa-rewrites-repeat-v1"
        ),
        "captured_at": datetime.now(UTC).isoformat(),
        "model": settings.DEFAULT_MODEL,
        "processor": "servicemind.rag.query.QueryProcessor",
        "model_query_mode": mode,
        "model_query_source": (
            "Phase5Governance.build_fast_knowledge_context -> ContextEnvelope.model_payload"
            if mode == "envelope"
            else "the bare question (model_query=None), re-run as the noise control"
        ),
        "envelope_constant_ticket_id": TICKET,
        "note": (
            "the rewrite stage's output when the model is given the deployed context "
            "envelope instead of the bare question. No secrets: the envelope holds the "
            "release question and the platform policy item only."
            if mode == "envelope"
            else "the same stage re-run on the same input, as the noise control for the "
            "envelope comparison. The stage is sampled, so this is what 'changed' looks "
            "like when nothing changed."
        ),
        "capture_seconds": round(time.perf_counter() - started, 1),
        "concurrency": concurrency,
        "queries": captured,
    }
    if limit is not None:
        # A limited pass is a look at the shape, not a cache: writing it would leave a
        # partial capture on disk that the reuse check happens to reject today and a
        # later edit could accept.
        print(f"limited to {limit} queries; nothing written")
        return payload
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"captured {len(captured)} {mode} rewrites in {payload['capture_seconds']}s")
    return payload


def _report(capture_payload: dict[str, Any]) -> tuple[Any, dict[str, Any], dict[str, Any]]:
    """The envelope comparison, plus the same comparison on a repeat of the same input.

    The control is not optional. The rewrite stage samples, so a difference between the
    envelope pass and the bare-question pass is only evidence about the *input* if
    re-running the bare question alone reproduces itself. A missing control raises rather
    than being omitted: dropping it would leave a number that reads like a finding.
    """
    funnel = _load(FUNNEL_SCRIPT, "phase4_query_arms_measure")
    plain = json.loads(PLAIN_CAPTURE.read_text(encoding="utf-8"))
    if not REPEAT_CAPTURE.exists():
        raise FileNotFoundError(
            f"{REPEAT_CAPTURE} is missing: run "
            "`probe_phase4_envelope_query_arm.py --capture-only --repeat` first. Without "
            "it the envelope comparison has no noise floor and cannot be read."
        )
    repeat = json.loads(REPEAT_CAPTURE.read_text(encoding="utf-8"))
    return funnel, _comparison(plain, capture_payload), _comparison(plain, repeat)


async def _measurable_corpus(client: Any, index: OpenSearchKnowledgeIndex) -> tuple[str, int, int]:
    """Refuse an index that would report zero for a reason the payload cannot show.

    This probe never ingests, so the index under ``--index-prefix`` *is* the corpus. An
    alias that no longer resolves, or a live alias whose parent generation is empty,
    makes every arm report zero -- and a zero written into the payload reads as a
    measurement, not as a missing index. The funnel this probe borrows its reuse
    substitute from refuses both states for exactly that reason; borrowing the
    substitute without the refusal is how a probe reports on nothing.
    """
    active = await index.active_generation(TENANT)
    if active is None:
        raise RuntimeError(
            f"no active generation for tenant {TENANT}: the probe never ingests, so an "
            "unreachable index means it would measure nothing and say so as a zero"
        )
    children = int((await client.count(index=index.child_alias(TENANT))).get("count", 0))
    parents = int((await client.count(index=index.parent_alias(TENANT))).get("count", 0))
    print(f"active generation {active}: {children} children, {parents} parents", flush=True)
    if parents == 0:
        raise RuntimeError(
            f"the active parent generation for {TENANT} is empty: the arms would search "
            "and then drop every hit at parent expansion, which reports as recall 0"
        )
    return active, children, parents


async def measure(*, concurrency: int, force: bool, index_prefix: str) -> dict[str, Any]:
    funnel, comparison, noise = _report(await capture(concurrency=concurrency, force=force))
    release = funnel._load_release_module()
    selection = json.loads(SELECTION.read_text(encoding="utf-8"))
    gold = release._gold_set(
        selection, json.loads((DATA / "train.json").read_text(encoding="utf-8"))
    )
    documents = release._corpus(None)

    from core import settings

    embedding = TeiEmbeddingProvider(
        settings.SERVICEMIND_EMBEDDING_URL,
        model_revision=settings.SERVICEMIND_EMBEDDING_REVISION,
    )
    reranker = TeiReranker(
        settings.SERVICEMIND_RERANKER_URL,
        model_revision=settings.SERVICEMIND_RERANKER_REVISION,
    )
    client = build_opensearch_client()
    index = OpenSearchKnowledgeIndex(client, prefix=index_prefix, dimension=embedding.dimension)
    active, children_on_cluster, parents_on_cluster = await _measurable_corpus(client, index)
    # Same substitute as the funnel's reuse path: this probe never ingests, so an
    # ingest-filled in-memory parent store would be empty and drop every hit.
    rag = EnterpriseRAG(
        index=index,
        embedding=embedding,
        reranker=reranker,
        repository=funnel.KeptIndexRepository(index),  # type: ignore[arg-type]
    )
    principal = funnel.RetrievalPrincipal(
        tenant_id=TENANT, user_id="phase4-techqa-envelope-arm", entity_ids=frozenset({1})
    )
    coordinates = release._coordinates(documents)

    import servicemind.rag.service as service

    envelope_capture = json.loads(ENVELOPE_CAPTURE.read_text(encoding="utf-8"))
    original = service.query_processor

    def _replay(source: str, *, keep_rewrites: bool):
        payload = (
            envelope_capture
            if source == "envelope"
            else json.loads(PLAIN_CAPTURE.read_text(encoding="utf-8"))
        )
        return funnel.CannedQueryProcessor(
            {
                entry["query"]: funnel._knowledge_query(
                    entry, entry["query"], keep_rewrites=keep_rewrites
                )
                for entry in payload["queries"].values()
            }
        )

    arms: dict[str, Any] = {}
    try:
        for arm in ARMS:
            source = CAPTURE_OF[arm["name"]]
            service.query_processor = _replay(source, keep_rewrites=arm["use_rewrites"])
            provider = funnel._ArmProvider(rag, arm)
            started = time.perf_counter()
            report = await funnel.evaluate(
                provider,
                gold,
                principal,
                top_ks=funnel.TOP_KS,
                baselines=(funnel.PRODUCTION,),
                coordinates=coordinates,
            )
            elapsed = time.perf_counter() - started
            metrics = report.metrics(funnel.PRODUCTION.name)
            arms[arm["name"]] = {
                **funnel._arm_metrics(metrics, elapsed),
                "use_query_model": arm["use_query_model"],
                "use_rewrites": arm["use_rewrites"],
                "reading": arm["reading"],
                "rewrite_capture": source,
            }
            print(
                f"{arm['name']:>10}  R@10={arms[arm['name']]['recall_at_10']:.4f} ({elapsed:.0f}s)",
                flush=True,
            )
    finally:
        service.query_processor = original

    published = json.loads(FUNNEL_REPORT.read_text(encoding="utf-8"))
    payload: dict[str, Any] = {
        "schema_version": "phase4-envelope-query-arm-v1",
        "status": "ENVELOPE_QUERY_ARM_MEASUREMENT",
        "quality_certification": False,
        "generated_at": datetime.now(UTC).isoformat(),
        "status_semantics": (
            "this measures the third gap between the committed TechQA numbers and the "
            "deployed pipeline: the *input* the rewrite stage is given. The labels remain "
            "silver, so it certifies nothing about tenant-domain quality."
        ),
        "inputs": {
            "producer": {
                "path": str(PRODUCER.relative_to(ROOT)),
                "sha256": _digest(PRODUCER),
                "tracked": True,
            },
            "envelope_capture": {
                "path": str(ENVELOPE_CAPTURE.relative_to(ROOT)),
                "sha256": _digest(ENVELOPE_CAPTURE),
                "model": envelope_capture["model"],
                "tracked": False,
            },
            "plain_capture": {
                "path": str(PLAIN_CAPTURE.relative_to(ROOT)),
                "sha256": _digest(PLAIN_CAPTURE),
                "tracked": False,
            },
            "repeat_capture": {
                "path": str(REPEAT_CAPTURE.relative_to(ROOT)),
                "sha256": _digest(REPEAT_CAPTURE),
                "tracked": False,
                "note": (
                    "the bare-question pass re-run on the same input. It is the control "
                    "for every number in rewrite_sensitivity, not a second measurement."
                ),
            },
            "funnel_report": {
                "path": str(FUNNEL_REPORT.relative_to(ROOT)),
                "sha256": _digest(FUNNEL_REPORT),
            },
            "index_prefix": index_prefix,
            "index_rebuilt": False,
        },
        "corpus": {
            "active_generation": active,
            "children_on_cluster": children_on_cluster,
            "parents_on_cluster": parents_on_cluster,
            "rebuilt_here": False,
        },
        "envelope_input": {
            "call_site": "src/servicemind/orchestration/supervisor_workflow.py:1000",
            "builder": "Phase5Governance.build_fast_knowledge_context",
            "context_enabled_default": settings.SERVICEMIND_CONTEXT_ENABLED,
            "deployed_value": "true (deploy/glpi/.env(.example))",
        },
        "rewrite_sensitivity": {
            **comparison,
            "noise_floor": noise,
            "noise_floor_reading": (
                "the same comparison run on a repeat of the *same* input. Anything the "
                "envelope number does not exceed this by is sampling, not the input."
            ),
        },
        "arms": arms,
        "published_funnel_arms": {
            name: {
                "recall_at_10": row["recall_at_10"],
                "packed_max": row["packed_documents"]["max"],
            }
            for name, row in published["arms"].items()
        },
        "limitations": [
            "the envelope reproduced here is the *fast knowledge* path's; the "
            "knowledge-task path assembles memory and ticket items the release set does "
            "not have, so it is not reproducible off-line",
            "the ticket id in the task item is a constant: the release set has no GLPI tickets",
            "the labels are source-provided silver, not tenant-domain human qrels",
            "the rewrite capture is one sample of a non-deterministic stage; the vendored "
            "sha256 is what makes this sample reproducible, not the stage",
            "the index is the kept one from the query-arm run, so these rows share its "
            "generation by construction; an unreachable or empty generation is refused "
            "rather than measured, but a *stale* one -- a kept index whose generation no "
            "longer matches the query-arm report's -- is not detected here",
            "parent expansion reads the index's parent alias rather than PostgreSQL "
            "under row-level security, so this run exercises the expansion call, its "
            "tenant scoping and its ACL filter, but not the RLS policy itself",
        ],
    }
    return payload


def render_markdown(payload: dict[str, Any]) -> str:
    arms = payload["arms"]
    sensitivity = payload["rewrite_sensitivity"]
    noise = sensitivity["noise_floor"]
    published = payload["published_funnel_arms"]
    rows = []
    for name, arm in arms.items():
        reference = published.get("c0_off", {}).get("recall_at_10")
        delta = f"{arm['recall_at_10'] - reference:+.4f}" if reference is not None else "—"
        rows.append(
            f"| `{name}` | {arm['rewrite_capture']} | "
            f"{'on' if arm['use_rewrites'] else 'off'} | "
            f"{arm['recall_at_5'] * 100:.2f}% | {arm['recall_at_10'] * 100:.2f}% | "
            f"{arm['recall_at_20'] * 100:.2f}% | {delta} | {arm['packed_documents']['max']} |"
        )
    examples = "\n".join(
        f"| {item['query'][:48]} | {item['bare_question_normalised'][:60]} | "
        f"{item['envelope_normalised'][:60]} |"
        for item in sensitivity["examples"]
    )
    return f"""# 改写阶段的输入差：裸问题 vs 部署信封

**状态**：`{payload["status"]}`（不构成质量认证）
**生成时间**：{payload["generated_at"]}
**索引**：`{payload["inputs"]["index_prefix"]}`（**不重建**，沿用 query arm 那轮留下的代际）
**代际**：`{payload["corpus"]["active_generation"]}`（{payload["corpus"]["children_on_cluster"]} children / {payload["corpus"]["parents_on_cluster"]} parents，为空则拒绝运行）

## 这一轮补的是哪一段

已提交的数字与部署管道之间有三个差：`use_query_model`、`use_rewrites`，以及**改写阶段的输入**。
前两个由 `{payload["inputs"]["funnel_report"]["path"]}` 量化。第三个是这一份：

```python
{payload["envelope_input"]["call_site"]}
    kwargs["model_query"] = json.dumps(context_envelope.model_payload(), ...)
```

`QueryProcessor.process` 送给模型的是 `model_query or query`，所以只要平台装配了上下文信封，
模型拿到的就是**一个 JSON 数组**，用户的提问只是其中 `items[0].content` 里的 `goal` 字段。
`SERVICEMIND_CONTEXT_ENABLED` 默认 `False`，但部署把它设成 `{payload["envelope_input"]["deployed_value"]}`，
所以部署管道走的是这一支。

本探针**用平台自己的代码**复现该信封（`{payload["envelope_input"]["builder"]}` +
真实 `ContextBuilder` + `NullContextArtifactSink`），不手搓形状。

## 信封输入把模型的输出推开了多少

比的是**模型自己的字段**（`model_normalized_query` + `rewritten_queries`），也就是词法臂实际拿去搜的文本。
密集锚点两种输入下都是用户原话，信封推不动它；若这一列是 0，说明输入根本没送到模型。

| 项 | 信封 vs 裸提问 | **噪声对照**（同输入重跑） |
|---|---|---|
| 比对条数 | {sensitivity["queries_compared"]} | {noise["queries_compared"]} |
| 模型未作答（回退，已剔除） | {sensitivity["queries_without_a_model_answer"]} | {noise["queries_without_a_model_answer"]} |
| 模型输出改变 | **{sensitivity["model_output_changed"]}（{sensitivity["model_output_changed_share"] * 100:.2f}%）** | {noise["model_output_changed"]}（{noise["model_output_changed_share"] * 100:.2f}%） |
| 其中仅改写列表改变 | {sensitivity["only_the_rewrites_changed"]} | {noise["only_the_rewrites_changed"]} |
| 完全不变 | {sensitivity["unchanged"]} | {noise["unchanged"]} |

改写阶段是采样的，所以左列必须先超过右列才说明问题；右列是**完全相同的输入**重跑一遍。

{sensitivity["reading"]}

| 提问 | 裸问题改写 | 信封改写 |
|---|---|---|
{examples}

## 各 arm 实测

| arm | 改写输入 | fan-out | R@5 | R@10 | R@20 | 对 `c0_off` | packed max |
|---|---|---|---|---|---|---|---|
{chr(10).join(rows)}

## 局限

{chr(10).join(f"- {item}" for item in payload["limitations"])}
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-only", action="store_true")
    parser.add_argument("--force-capture", action="store_true")
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--index-prefix", default="sm-techqa-arms-v1")
    parser.add_argument(
        "--limit", type=int, default=None, help="capture a prefix of the query set and stop"
    )
    parser.add_argument(
        "--repeat",
        action="store_true",
        help="capture the bare-question pass again as the comparison's noise control",
    )
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    if args.check:
        if not (OUT_JSON.exists() and OUT_MD.exists()):
            print("FAIL report missing")
            return 1
        payload = json.loads(OUT_JSON.read_text(encoding="utf-8"))
        stale = [
            key
            for key, path in (
                ("producer", PRODUCER),
                ("envelope_capture", ENVELOPE_CAPTURE),
                ("plain_capture", PLAIN_CAPTURE),
                ("repeat_capture", REPEAT_CAPTURE),
                ("funnel_report", FUNNEL_REPORT),
            )
            if payload["inputs"][key]["sha256"] != _digest(path)
        ]
        markdown_matches = OUT_MD.read_text(encoding="utf-8") == render_markdown(payload)
        ok = not stale and markdown_matches
        print(
            "PASS" if ok else "FAIL",
            payload["status"],
            f"stale_inputs={stale}",
            f"markdown_matches={markdown_matches}",
        )
        return 0 if ok else 1

    loop = lambda: asyncio.SelectorEventLoop(selectors.SelectSelector())  # noqa: E731
    if args.capture_only:
        captured = asyncio.run(
            capture(
                concurrency=args.concurrency,
                force=args.force_capture,
                limit=args.limit,
                mode="plain" if args.repeat else "envelope",
            ),
            loop_factory=loop,
        )
        plain = json.loads(PLAIN_CAPTURE.read_text(encoding="utf-8"))
        comparison = _comparison(plain, captured)
        comparison["compared_against"] = (
            "the bare-question capture, re-run (noise control)"
            if args.repeat
            else "the bare-question capture"
        )
        print(json.dumps(comparison, ensure_ascii=False, indent=2))
        return 0

    payload = asyncio.run(
        measure(
            concurrency=args.concurrency,
            force=args.force_capture,
            index_prefix=args.index_prefix,
        ),
        loop_factory=loop,
    )
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    OUT_MD.write_text(render_markdown(payload), encoding="utf-8")
    print("wrote", OUT_JSON.name, "and", OUT_MD.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
