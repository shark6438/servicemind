from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from typing import Protocol

import tiktoken

from servicemind.domain.knowledge import (
    ChildChunk,
    KnowledgeBlock,
    KnowledgeDocument,
    ParentChunk,
)


class AsyncEmbeddingProvider(Protocol):
    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...


class StructureAwareSemanticChunker:
    def __init__(
        self,
        *,
        child_min_tokens: int = 120,
        child_target_tokens: int = 320,
        child_max_tokens: int = 480,
        parent_max_tokens: int = 1500,
        parent_max_chars: int = 7000,
        semantic_threshold: float = 0.55,
    ) -> None:
        self.child_min_tokens = child_min_tokens
        self.child_target_tokens = child_target_tokens
        self.child_max_tokens = child_max_tokens
        self.parent_max_tokens = parent_max_tokens
        #: Character ceiling for a stored parent. A retrieved parent is serialized
        #: verbatim as the Evidence content row, which caps at EVIDENCE_CONTENT_MAX
        #: (8000) chars; a parent must never exceed it or retrieval would crash on
        #: serialization. Token budgets alone cannot guarantee this -- a single parsed
        #: block (e.g. one whole ticket thread as a LIST block) can be tens of KB.
        self.parent_max_chars = parent_max_chars
        self.semantic_threshold = semantic_threshold
        self.encoder = tiktoken.get_encoding("cl100k_base")

    def _split_long_text(self, text: str, limit: int) -> list[str]:
        """Bound ``text`` to ``limit`` chars, cutting at paragraph/newline boundaries.

        Prefers whole paragraphs, then whole lines, and only falls back to a hard
        character cut for a single over-long line. Every returned piece is stripped and
        is at most ``limit`` chars (they may be far shorter -- the boundary wins).
        """
        paragraphs = re.split(r"(\n{2,})", text)
        pieces: list[str] = []
        buffer = ""
        for chunk in paragraphs:
            if not chunk:
                continue
            if re.fullmatch(r"\n{2,}", chunk):
                buffer += chunk
                continue
            if buffer.strip() and len(buffer) + len(chunk) > limit:
                pieces.append(buffer.rstrip("\n"))
                buffer = ""
            if len(chunk) <= limit:
                buffer += chunk
                continue
            for line in chunk.split("\n"):
                candidate = f"{buffer}\n{line}" if buffer else line
                if buffer and len(candidate) > limit:
                    pieces.append(buffer)
                    buffer = ""
                    candidate = line
                while len(candidate) > limit:
                    pieces.append(candidate[:limit])
                    candidate = candidate[limit:]
                buffer = candidate
        if buffer.strip():
            pieces.append(buffer.rstrip("\n"))
        return [piece for piece in pieces if piece.strip()]

    def tokens(self, text: str) -> int:
        return max(len(self.encoder.encode(text)), 1)

    def _cosine(self, left: Sequence[float], right: Sequence[float]) -> float:
        dot = sum(a * b for a, b in zip(left, right, strict=True))
        lnorm = math.sqrt(sum(value * value for value in left))
        rnorm = math.sqrt(sum(value * value for value in right))
        return dot / (lnorm * rnorm) if lnorm and rnorm else 0

    def build_parents(self, blocks: list[KnowledgeBlock]) -> list[ParentChunk]:
        parents: list[ParentChunk] = []
        current: list[KnowledgeBlock] = []
        current_tokens = 0
        current_section: list[str] = []

        def flush() -> None:
            nonlocal current, current_tokens
            if not current:
                return
            content = "\n\n".join(item.content for item in current)
            section = current_section.copy()
            block_ids = [item.block_id for item in current]
            if len(content) > self.parent_max_chars:
                # A single parsed block can be arbitrarily long (one whole ticket
                # thread); emit it as consecutive bounded parents so every parent
                # stays below the platform evidence content ceiling.
                pieces = self._split_long_text(content, self.parent_max_chars)
            else:
                pieces = [content]
            start = len(parents)
            for index, piece in enumerate(pieces):
                parents.append(
                    ParentChunk(
                        document_id=current[0].document_id,
                        section_path=section,
                        block_ids=block_ids,
                        order=start + index,
                        content=piece,
                        token_count=self.tokens(piece),
                    )
                )
            current, current_tokens = [], 0

        for block in blocks:
            block_tokens = self.tokens(block.content)
            section_changed = current and block.section_path != current_section
            if section_changed or (
                current and current_tokens + block_tokens > self.parent_max_tokens
            ):
                flush()
            if not current:
                current_section = block.section_path.copy()
            current.append(block)
            current_tokens += block_tokens
        flush()
        return parents

    def _hard_split(self, text: str) -> list[str]:
        tokens = self.encoder.encode(text)
        return [
            self.encoder.decode(tokens[index : index + self.child_max_tokens])
            for index in range(0, len(tokens), self.child_max_tokens)
        ]

    async def _semantic_segments(self, text: str, embedding: AsyncEmbeddingProvider) -> list[str]:
        sentences = [
            item.strip() for item in re.split(r"(?<=[.!?。！？])\s+", text) if item.strip()
        ]
        if len(sentences) < 2:
            return self._hard_split(text) if self.tokens(text) > self.child_max_tokens else [text]
        vectors = await embedding.embed_documents(sentences)
        segments: list[str] = []
        current = sentences[0]
        for index, sentence in enumerate(sentences[1:], start=1):
            candidate = f"{current} {sentence}"
            similarity = self._cosine(vectors[index - 1], vectors[index])
            should_break = self.tokens(current) >= self.child_min_tokens and (
                similarity < self.semantic_threshold
                or self.tokens(candidate) > self.child_target_tokens
            )
            if should_break:
                segments.append(current)
                current = sentence
            else:
                current = candidate
        segments.append(current)
        return [piece for segment in segments for piece in self._hard_split(segment)]

    async def build_children(
        self,
        document: KnowledgeDocument,
        parents: list[ParentChunk],
        embedding: AsyncEmbeddingProvider,
    ) -> list[ChildChunk]:
        children: list[ChildChunk] = []
        for parent in parents:
            if parent.token_count <= self.child_max_tokens:
                segments = [parent.content]
            elif "<table" in parent.content.casefold() or "```" in parent.content:
                segments = self._hard_split(parent.content)
            else:
                segments = await self._semantic_segments(parent.content, embedding)
            for content in segments:
                children.append(
                    ChildChunk(
                        parent_chunk_id=parent.parent_chunk_id,
                        document_id=document.document_id,
                        section_path=parent.section_path,
                        order=len(children),
                        content=content,
                        token_count=self.tokens(content),
                        content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    )
                )
        return children


semantic_chunker = StructureAwareSemanticChunker()
