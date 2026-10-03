"""The GPU-side encoder for the late-interaction arm.

This module is imported by the encoder process, never by the API: the repo
virtualenv pins ``torch 2.13.0+cu130`` while the host driver is CUDA 12.2, so
``torch.cuda.is_available()`` is False there. The encoder therefore runs in the
interpreter that can see the cards (``SERVICEMIND_LI_PYTHON``), and the platform
reaches it the same way it reaches TEI -- over HTTP.

The head is ``colbert_linear.pt`` from the pinned BGE-M3 snapshot: a plain
``nn.Linear(1024, 1024)`` applied to the last hidden state. Loading it by hand
rather than through the model's remote code keeps ``trust_remote_code=False``
true, which the plan requires.

Shared cards
------------
The two 3090s belong to the group, not to this project. ``memory_fraction``
caps what this process may take, and the device is chosen by *current* free
memory rather than pinned, so the encoder steps aside when someone else is
using a card.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from servicemind.rag.late_interaction import (
    DEFAULT_MAX_DOC_TOKENS,
    DEFAULT_MAX_QUERY_TOKENS,
)

logger = logging.getLogger("servicemind.rag.late_interaction_torch")

MODEL_NAME = "BAAI/bge-m3"
COLBERT_HEAD = "colbert_linear.pt"


def choose_device(torch: Any) -> tuple[Any, str]:
    """Return ``(device, description)`` for the card with the most free memory.

    Choosing at call time instead of freezing ``CUDA_VISIBLE_DEVICES`` means a
    busy card is stepped around rather than discovered as an OOM.
    """
    if not torch.cuda.is_available():
        raise RuntimeError(
            "this interpreter cannot see a CUDA device; the encoder must run under an "
            "interpreter whose torch matches the host driver"
        )
    best_index, best_free = 0, -1
    for index in range(torch.cuda.device_count()):
        free, total = torch.cuda.mem_get_info(index)
        logger.info("gpu%d free %.1f GiB of %.1f GiB", index, free / 2**30, total / 2**30)
        if free > best_free:
            best_index, best_free = index, free
    return torch.device(f"cuda:{best_index}"), f"cuda:{best_index}"


def _load_backbone(torch: Any, snapshot_dir: Path) -> Any:
    """Instantiate the encoder from its config and load the checkpoint ourselves.

    ``transformers`` refuses to call ``torch.load`` at all on torch < 2.6, even
    with ``weights_only=True`` (CVE-2025-32434), and the interpreter that can see
    the cards here is torch 2.5.1. Upgrading torch on a shared group machine is
    not ours to do, so we build the module from ``config.json`` and load
    ``pytorch_model.bin`` directly. The checkpoint is a plain XLMRobertaModel
    state dict, which the constructor checks by demanding a key-for-key match --
    a silently partial load would produce plausible-looking but wrong vectors.
    """
    from transformers import AutoConfig, AutoModel

    config = AutoConfig.from_pretrained(
        str(snapshot_dir), trust_remote_code=False, local_files_only=True
    )
    model = AutoModel.from_config(config)
    state = torch.load(snapshot_dir / "pytorch_model.bin", map_location="cpu", weights_only=True)
    result = model.load_state_dict(state, strict=False)
    if result.missing_keys or result.unexpected_keys:
        raise RuntimeError(
            "backbone checkpoint does not match the architecture: "
            f"{len(result.missing_keys)} missing, {len(result.unexpected_keys)} unexpected; "
            f"first missing={result.missing_keys[:3]}, first unexpected={result.unexpected_keys[:3]}"
        )
    return model


@dataclass(frozen=True, slots=True)
class EncodedBatch:
    """One encoded document or query: L2-normalised token vectors, no padding."""

    vectors: np.ndarray  # (tokens, dimension) float32
    tokens: int


class BgeM3MultiVectorEncoder:
    """BGE-M3's token-level head, loaded from a pinned local snapshot."""

    def __init__(
        self,
        *,
        snapshot_dir: str | Path,
        revision: str,
        device: str | None = None,
        memory_fraction: float = 0.4,
        max_doc_tokens: int = DEFAULT_MAX_DOC_TOKENS,
        max_query_tokens: int = DEFAULT_MAX_QUERY_TOKENS,
    ) -> None:
        import torch

        self.torch = torch
        self.revision = revision
        self.max_doc_tokens = max_doc_tokens
        self.max_query_tokens = max_query_tokens
        self.snapshot_dir = Path(snapshot_dir)
        if not (self.snapshot_dir / "config.json").is_file():
            raise FileNotFoundError(f"{self.snapshot_dir} is not a model snapshot")
        head_path = self.snapshot_dir / COLBERT_HEAD
        if not head_path.is_file():
            raise FileNotFoundError(f"{head_path} is missing; this snapshot has no ColBERT head")

        if device is None:
            self.device, description = choose_device(torch)
        else:
            self.device, description = torch.device(device), device
        if self.device.type == "cuda":
            torch.cuda.set_per_process_memory_fraction(memory_fraction, self.device)
            logger.info(
                "encoder on %s, capped at %.0f%% of its memory",
                description,
                memory_fraction * 100,
            )

        from transformers import AutoTokenizer

        self.tokenizer = AutoTokenizer.from_pretrained(
            str(self.snapshot_dir), trust_remote_code=False, local_files_only=True
        )
        self.model = _load_backbone(torch, self.snapshot_dir)
        self.model.eval()
        self.model.to(self.device)

        state = torch.load(head_path, map_location="cpu", weights_only=True)
        head = torch.nn.Linear(state["weight"].shape[1], state["weight"].shape[0])
        with torch.no_grad():
            head.weight.copy_(state["weight"].float())
            head.bias.copy_(state["bias"].float())
        self.head = head.to(self.device).eval().requires_grad_(False)
        self.dimension = int(state["weight"].shape[0])

    def _token_vectors(self, texts: Sequence[str], max_tokens: int) -> list[np.ndarray]:
        """Encode a batch, returning one ``(tokens, dimension)`` array per input.

        Padding is removed before returning, so a document's stored vectors do
        not depend on which other documents shared its batch.
        """
        torch = self.torch
        encoded = self.tokenizer(
            list(texts),
            return_tensors="pt",
            truncation=True,
            max_length=max_tokens,
            padding=True,
        )
        mask = encoded["attention_mask"].to(self.device)
        encoded = {key: value.to(self.device) for key, value in encoded.items()}
        with torch.inference_mode():
            hidden = self.model(**encoded).last_hidden_state
            # Mirror the checkpoint's own reference implementation: the head sees
            # content tokens, so the leading CLS is dropped and padding never
            # reaches the linear layer.
            token_vectors = self.head(hidden[:, 1:]).float()
            norms = token_vectors.norm(dim=-1, keepdim=True).clamp_min(1e-8)
            normalised = (token_vectors / norms).cpu().numpy()
        content = mask[:, 1:].bool()
        return [
            normalised[row][content[row].cpu().numpy()].astype(np.float32)
            for row in range(len(texts))
        ]

    def encode_documents(self, texts: Sequence[str], *, batch_size: int = 16) -> list[EncodedBatch]:
        """Encode documents in batches; order is preserved."""
        out: list[EncodedBatch] = []
        for start in range(0, len(texts), batch_size):
            chunk = list(texts[start : start + batch_size])
            if not chunk:
                continue
            for vectors in self._token_vectors(chunk, self.max_doc_tokens):
                out.append(EncodedBatch(vectors, int(vectors.shape[0])))
        return out

    def encode_query(self, text: str) -> EncodedBatch:
        """Encode one query.

        Queries are encoded one at a time on purpose: batching changes a row's
        result in the last bits depending on its batch neighbours, and the plan
        requires a repeated identical query to reproduce the identical
        candidate set *and order*.
        """
        vectors = self._token_vectors([text], self.max_query_tokens)[0]
        return EncodedBatch(vectors, int(vectors.shape[0]))
