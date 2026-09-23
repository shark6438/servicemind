"""Deterministic integrity primitives owned by the domain layer."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel


def stable_digest(value: Any) -> str:
    """Return a deterministic digest for a Pydantic or JSON-compatible value."""

    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


#: One canonical deterministic prompt-injection vocabulary, shared by every boundary
#: that decides what untrusted text may reach a model: the memory write policy, the
#: context envelope, and anything added later.
#:
#: It is declared here, in the domain layer that both of those already depend on, for
#: the same reason it exists at all. It used to live in ``memory.policy``, whose own
#: comment claimed it was "shared by every model-facing policy boundary" -- but the
#: context envelope could not reach it without importing the memory package, so the
#: boundary that hands retrieved text straight to the analysis model had no tripwire
#: and the claim was false. A vocabulary two boundaries are supposed to share cannot
#: be filed under one of them.
#:
#: Matching is a substring test on case-folded text, so it fires on a document that
#: *quotes* an injection as readily as on one that attempts it. That is the intended
#: trade: a false positive costs one withheld document and a recorded reason an
#: operator can see, and a false negative costs the run. Callers fail closed.
INJECTION_MARKERS = (
    "ignore previous",
    "ignore all prior",
    "override policy",
    "bypass approval",
    "system prompt",
    "ignore your instructions",
    "ignore all previous",
    "disregard previous",
    "disregard all prior",
    "override the system",
    "you are now",
    "act as the system",
    "act as the assistant",
    "developer message",
    "system message",
    "forget previous",
    "忽略所有",
    "忽略系统提示",
    "绕过审批",
    "无视系统",
    "无视之前",
    "现在扮演",
    "跳过审批",
)


def contains_injection_marker(value: str) -> bool:
    """Return whether untrusted text hits the canonical deterministic tripwire."""
    normalized = value.casefold()
    return any(marker in normalized for marker in INJECTION_MARKERS)
