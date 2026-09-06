from __future__ import annotations

import re
from pathlib import Path

from bs4 import BeautifulSoup
from docling.document_converter import DocumentConverter
from docling_core.types.doc import SectionHeaderItem, TableItem, TextItem

from servicemind.domain.knowledge import BlockType, KnowledgeBlock, KnowledgeDocument


class StructureParser:
    """Recover typed blocks before chunking; no model-visible content is executed."""

    def __init__(self) -> None:
        self._docling: DocumentConverter | None = None

    def parse_markdown(self, document: KnowledgeDocument) -> list[KnowledgeBlock]:
        lines = document.content.splitlines()
        blocks: list[KnowledgeBlock] = []
        section_path: list[str] = []
        buffer: list[str] = []
        current_type = BlockType.PARAGRAPH
        in_fence = False

        def flush() -> None:
            nonlocal buffer
            content = "\n".join(buffer).strip()
            if content:
                blocks.append(
                    KnowledgeBlock(
                        document_id=document.document_id,
                        section_path=section_path.copy(),
                        order=len(blocks),
                        block_type=current_type,
                        content=content,
                    )
                )
            buffer = []

        for line in lines:
            stripped = line.strip()
            heading = re.match(r"^(#{1,6})\s+(.+)$", stripped)
            if stripped.startswith("```"):
                if in_fence:
                    buffer.append(line)
                    flush()
                    in_fence = False
                    current_type = BlockType.PARAGRAPH
                else:
                    flush()
                    current_type = BlockType.CODE
                    in_fence = True
                    buffer.append(line)
                continue
            if in_fence:
                buffer.append(line)
                continue
            if heading:
                flush()
                level, title = len(heading.group(1)), heading.group(2).strip()
                section_path = section_path[: level - 1] + [title]
                blocks.append(
                    KnowledgeBlock(
                        document_id=document.document_id,
                        section_path=section_path.copy(),
                        order=len(blocks),
                        block_type=BlockType.HEADING,
                        content=title,
                        metadata={"level": level},
                    )
                )
                continue
            detected = (
                BlockType.TABLE
                if stripped.startswith("|") and "|" in stripped[1:]
                else BlockType.LIST
                if re.match(r"^(?:[-*+]\s+|\d+[.)]\s+)", stripped)
                else BlockType.PARAGRAPH
            )
            if stripped == "":
                flush()
                current_type = BlockType.PARAGRAPH
            elif detected != current_type and buffer:
                flush()
                current_type = detected
                buffer.append(line)
            else:
                current_type = detected
                buffer.append(line)
        flush()
        return blocks

    def parse_html(self, document: KnowledgeDocument) -> list[KnowledgeBlock]:
        soup = BeautifulSoup(document.content, "html.parser")
        for node in soup(["script", "style", "iframe", "object"]):
            node.decompose()
        blocks: list[KnowledgeBlock] = []
        section_path: list[str] = []
        for node in soup.find_all(
            ["h1", "h2", "h3", "h4", "h5", "h6", "p", "ul", "ol", "table", "pre"]
        ):
            if node.find_parent(["ul", "ol", "table", "pre"]) is not None:
                continue
            name = node.name.casefold()
            if name.startswith("h"):
                level = int(name[1])
                text = node.get_text(" ", strip=True)
                section_path = section_path[: level - 1] + [text]
                kind = BlockType.HEADING
                metadata = {"level": level}
            elif name == "table":
                text, kind, metadata = str(node), BlockType.TABLE, {"format": "html"}
            elif name in {"ul", "ol"}:
                text, kind, metadata = node.get_text("\n", strip=True), BlockType.LIST, {}
            elif name == "pre":
                text, kind, metadata = node.get_text("\n", strip=False), BlockType.CODE, {}
            else:
                text, kind, metadata = node.get_text(" ", strip=True), BlockType.PARAGRAPH, {}
            if text:
                blocks.append(
                    KnowledgeBlock(
                        document_id=document.document_id,
                        section_path=section_path.copy(),
                        order=len(blocks),
                        block_type=kind,
                        content=text,
                        metadata=metadata,
                    )
                )
        return blocks

    def parse_file(self, path: Path, document: KnowledgeDocument) -> list[KnowledgeBlock]:
        suffix = path.suffix.casefold()
        if suffix in {".md", ".markdown"}:
            return self.parse_markdown(document)
        if suffix in {".html", ".htm"}:
            return self.parse_html(document)
        if suffix in {".txt", ".text"}:
            return [
                KnowledgeBlock(
                    document_id=document.document_id,
                    order=0,
                    block_type=BlockType.PARAGRAPH,
                    content=document.content,
                )
            ]
        if self._docling is None:
            self._docling = DocumentConverter()
        converted = self._docling.convert(path).document
        blocks: list[KnowledgeBlock] = []
        section_path: list[str] = []
        for item, level in converted.iterate_items():
            if isinstance(item, SectionHeaderItem):
                title = item.text.strip()
                section_path = section_path[: max(level - 1, 0)] + [title]
                kind, content, metadata = BlockType.HEADING, title, {"level": level}
            elif isinstance(item, TableItem):
                kind, content, metadata = (
                    BlockType.TABLE,
                    item.export_to_html(converted),
                    {"format": "html", "docling_type": type(item).__name__},
                )
            elif isinstance(item, TextItem):
                kind = (
                    BlockType.LIST
                    if "list" in type(item).__name__.casefold()
                    else BlockType.PARAGRAPH
                )
                content, metadata = item.text.strip(), {"docling_type": type(item).__name__}
            else:
                continue
            if content:
                blocks.append(
                    KnowledgeBlock(
                        document_id=document.document_id,
                        section_path=section_path.copy(),
                        order=len(blocks),
                        block_type=kind,
                        content=content,
                        metadata=metadata,
                    )
                )
        return blocks


structure_parser = StructureParser()
