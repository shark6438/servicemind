from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from uuid import UUID

import tiktoken

from servicemind.context.contracts import (
    DEFAULT_OUTPUT_RESERVE,
    DEFAULT_SYSTEM_RESERVE,
    ContextAgent,
    ContextBudget,
    ContextEnvelope,
    ContextItem,
    ContextSelection,
    ContextSource,
)
from servicemind.rag.chunking import ConservativeOfflineEncoding, has_cl100k_cache

ROLE_SOURCES: dict[ContextAgent, frozenset[ContextSource]] = {
    ContextAgent.DATA: frozenset(
        {ContextSource.TASK, ContextSource.STATE, ContextSource.TOOL_SCHEMA, ContextSource.POLICY}
    ),
    ContextAgent.KNOWLEDGE: frozenset(
        {ContextSource.TASK, ContextSource.STATE, ContextSource.POLICY}
    ),
    ContextAgent.ANALYSIS: frozenset(
        {
            ContextSource.TASK,
            ContextSource.EVIDENCE,
            ContextSource.MEMORY,
            ContextSource.SKILL,
            ContextSource.OUTPUT_SCHEMA,
            ContextSource.POLICY,
        }
    ),
    ContextAgent.REVIEWER: frozenset(
        {
            ContextSource.TASK,
            ContextSource.STATE,
            ContextSource.EVIDENCE,
            ContextSource.SKILL,
            ContextSource.OUTPUT_SCHEMA,
            ContextSource.POLICY,
        }
    ),
    ContextAgent.ACTION: frozenset(
        {ContextSource.TASK, ContextSource.STATE, ContextSource.TOOL_SCHEMA, ContextSource.POLICY}
    ),
}

SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passwd|secret|api[_ -]?key|private[_ -]?key|access[_ -]?token)\b"
    r"""(?P<key_quote>["']?)\s*(?P<separator>[:=])\s*"""
    r"""(?P<value_quote>["']?)([^\s"',;}]{4,})"""
)
SECRET_BLOB = re.compile(
    r"(?i)(?:"
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|\bBearer\s+[A-Za-z0-9._~+/=-]{12,}"
    r"|\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"
    r"|(?:postgres(?:ql)?|mysql|mongodb)://[^\s:/]+:[^\s@]+@"
    r")"
)
EMAIL = re.compile(r"(?<![\w.-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
PHONE = re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)")
BLOCKED_TAINTS = frozenset({"secret", "prompt_injection", "cross_tenant", "unresolved_taint"})


@dataclass(frozen=True)
class Redaction:
    text: str
    count: int


def redact_for_model(value: str) -> Redaction:
    count = 0

    def replace_secret(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return (
            f"{match.group(1)}{match.group('key_quote')}"
            f"{match.group('separator')}{match.group('value_quote')}[REDACTED_SECRET]"
        )

    value = SECRET_ASSIGNMENT.sub(replace_secret, value)
    value, blob_count = SECRET_BLOB.subn("[REDACTED_SECRET]", value)
    value, email_count = EMAIL.subn("[REDACTED_EMAIL]", value)
    value, phone_count = PHONE.subn("[REDACTED_PHONE]", value)
    return Redaction(text=value, count=count + blob_count + email_count + phone_count)


_default_encoding: tiktoken.Encoding | ConservativeOfflineEncoding | None = None


def _default_token_counter() -> Callable[[str], int]:
    """Return a cl100k_base-backed token counter, building the encoder lazily.

    tiktoken downloads the cl100k_base BPE table on first use and then caches it;
    the shared encoder is only resolved when this helper is first *called*, so
    importing or constructing a :class:`ContextBuilder` never blocks on the
    network.
    """
    global _default_encoding
    if _default_encoding is None:
        _default_encoding = (
            tiktoken.get_encoding("cl100k_base")
            if has_cl100k_cache()
            else ConservativeOfflineEncoding()
        )
    return lambda value: len(_default_encoding.encode(value))


class _DeferredTokenCounter:
    """Callable that resolves the shared encoder on first invocation, not at
    :class:`ContextBuilder` construction (which happens at import time for the
    module-level ``phase5_governance`` singleton)."""

    def __call__(self, value: str) -> int:
        return _default_token_counter()(value)


class ContextBuilder:
    """Build a minimal, role-scoped and token-bounded model input envelope."""

    def __init__(self, token_counter: Callable[[str], int] | None = None) -> None:
        self._token_counter = token_counter or _DeferredTokenCounter()

    def build(
        self,
        *,
        tenant_id: UUID,
        run_id: UUID,
        task_id: str,
        agent: ContextAgent,
        items: Iterable[ContextItem],
        max_input_tokens: int,
        system_reserve: int = DEFAULT_SYSTEM_RESERVE,
        output_reserve: int = DEFAULT_OUTPUT_RESERVE,
        source_token_caps: Mapping[ContextSource, int] | None = None,
    ) -> ContextEnvelope:
        allowed_sources = ROLE_SOURCES[agent]
        usable = max_input_tokens - system_reserve - output_reserve
        if usable <= 0:
            raise ValueError("context reserves leave no usable input budget")
        prepared: list[tuple[ContextItem, int]] = []
        manifest: list[ContextSelection] = []
        redaction_count = 0
        seen_hashes: set[str] = set()

        for item in items:
            if item.source not in allowed_sources or agent not in item.allowed_agents:
                manifest.append(
                    ContextSelection(
                        item_id=item.item_id,
                        source=item.source,
                        content_hash=item.content_hash,
                        tokens=0,
                        decision="rejected",
                        reason="agent_context_contract_denied",
                    )
                )
                continue
            if item.taint_labels & BLOCKED_TAINTS:
                manifest.append(
                    ContextSelection(
                        item_id=item.item_id,
                        source=item.source,
                        content_hash=item.content_hash,
                        tokens=0,
                        decision="rejected",
                        reason="unresolved_taint",
                    )
                )
                continue
            if item.content_hash in seen_hashes:
                manifest.append(
                    ContextSelection(
                        item_id=item.item_id,
                        source=item.source,
                        content_hash=item.content_hash,
                        tokens=0,
                        decision="pruned",
                        reason="exact_duplicate",
                    )
                )
                continue
            seen_hashes.add(item.content_hash)
            content = item.content
            if item.external_ref and self._token_counter(content) > 4096:
                content = (
                    f"[OFFLOADED content_hash={item.content_hash} external_ref={item.external_ref}]"
                )
            redaction = redact_for_model(content)
            redaction_count += redaction.count
            safe_item = item.model_copy(update={"content": redaction.text})
            prepared.append((safe_item, self._token_counter(safe_item.content)))

        prepared.sort(
            key=lambda pair: (
                not pair[0].required,
                -pair[0].authority,
                -pair[0].relevance,
                -pair[0].occurred_at.timestamp(),
                pair[0].item_id,
            )
        )
        selected: list[ContextItem] = []
        used = 0
        pruned = 0
        caps = dict(source_token_caps or {})
        invalid_caps = {source: cap for source, cap in caps.items() if cap < 0 or cap > usable // 2}
        if invalid_caps:
            raise ValueError(
                "source token caps must be non-negative and no more than half "
                f"the usable context budget: {invalid_caps}"
            )
        per_source: Counter[ContextSource] = Counter()
        for item, tokens in prepared:
            # A cap bounds how much of the envelope a *bulk* channel may claim; it must
            # never turn a required control item into a budget error, which would report
            # a channel-policy decision as an over-budget run.
            cap = None if item.required else caps.get(item.source)
            if used + tokens <= usable and (cap is None or per_source[item.source] + tokens <= cap):
                selected.append(item)
                used += tokens
                per_source[item.source] += tokens
                decision, reason = "selected", "ranked_within_budget"
            elif item.required:
                raise ValueError(f"required context item exceeds token budget: {item.item_id}")
            elif cap is not None and used + tokens <= usable:
                # Fits the budget but not its channel's share. Without this the largest
                # source simply takes the envelope and every lower-authority channel --
                # memory in particular, which is small and sorts last -- is starved with
                # no violation anywhere: the manifest calls the loss "pruned" and every
                # evaluation block that stops at retrieval still reads green.
                pruned += tokens
                decision, reason = "pruned", "source_token_cap_exceeded"
            else:
                pruned += tokens
                decision, reason = "pruned", "token_budget_exceeded"
            manifest.append(
                ContextSelection(
                    item_id=item.item_id,
                    source=item.source,
                    content_hash=item.content_hash,
                    tokens=tokens,
                    decision=decision,
                    reason=reason,
                )
            )
        return ContextEnvelope(
            tenant_id=tenant_id,
            run_id=run_id,
            task_id=task_id,
            agent=agent,
            items=tuple(selected),
            budget=ContextBudget(
                max_input_tokens=max_input_tokens,
                system_reserve=system_reserve,
                output_reserve=output_reserve,
                tokens_used=used,
                tokens_pruned=pruned,
            ),
            selection_manifest=tuple(manifest),
            redaction_count=redaction_count,
        )
