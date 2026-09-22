"""Offline-safe tokenization primitives shared by RAG and context assembly."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from collections.abc import Sequence
from pathlib import Path

_CL100K_URL = "https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken"


class ConservativeOfflineEncoding:
    """Lossless tokenizer that over-counts safely and never accesses the network."""

    _parts = re.compile(r"\s+|[\u4e00-\u9fff]|[A-Za-z0-9]{1,4}|[^A-Za-z0-9\s]")

    def encode(self, text: str) -> list[str]:
        return self._parts.findall(text)

    def decode(self, tokens: Sequence[str]) -> str:
        return "".join(tokens)


def has_cl100k_cache() -> bool:
    directory = (
        os.environ.get("TIKTOKEN_CACHE_DIR")
        or os.environ.get("DATA_GYM_CACHE_DIR")
        or str(Path(tempfile.gettempdir()) / "data-gym-cache")
    )
    key = hashlib.sha1(_CL100K_URL.encode()).hexdigest()
    return bool(directory) and (Path(directory) / key).is_file()
