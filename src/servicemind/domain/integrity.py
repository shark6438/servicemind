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
