from __future__ import annotations

import logging
import re
from pathlib import Path

from bs4 import BeautifulSoup
from docling.document_converter import DocumentConverter
from docling_core.types.doc import SectionHeaderItem, TableItem, TextItem

from servicemind.domain.knowledge import BlockType, KnowledgeBlock, KnowledgeDocument

logger = logging.getLogger("servicemind.rag.parsing")

#: Suffixes StructureParser consumes without invoking the document-conversion stack.
TEXT_SUFFIXES = frozenset({".md", ".markdown", ".html", ".htm", ".txt", ".text"})
#: PDFs go through docling when its layout model is reachable, else a pure-text
#: pypdf fallback. The layout model lives in a HuggingFace snapshot that is NOT part
#: of the wheel, so on an offline box the fallback is what keeps PDFs ingestible.
PDF_SUFFIXES = frozenset({".pdf"})
#: Office formats docling converts in-process without a downloaded PDF-layout model;
#: .docx additionally has a python-docx fallback so one bad convert never loses text.
DOCX_SUFFIXES = frozenset({".docx"})
BINARY_SUFFIXES = PDF_SUFFIXES | DOCX_SUFFIXES


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
        """Parse ``path`` by suffix; the caller (source loader / ingest) picked it.

        Text formats parse ``document.content`` (the loader already read the bytes in),
        so ``parse_file`` stays the single suffix-aware entry point whether a file is
        markdown, HTML, plain text or a binary attachment. Binary formats extract from
        the file path: docling first (lossless layout/reading order), with a pypdf
        fallback for PDFs whose layout-model snapshot is unreachable (offline boxes)
        and a python-docx fallback for .docx. The fallback is deliberately layout-less
        plain text -- honest degradation, never a fabricated structure.
        """
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
        if suffix in PDF_SUFFIXES:
            return self._pdf_blocks(path, document)
        if suffix in DOCX_SUFFIXES:
            return self._docx_blocks(path, document)
        raise ValueError(f"unsupported document suffix for ingest: {suffix or '(none)'}")

    def file_text(self, path: Path) -> str:
        """Archival plain text for a file, mirroring ``parse_file``'s suffix routing.

        Loaders store this as ``KnowledgeDocument.content`` so the content-digest
        invariant holds before ingest. Text formats yield their raw bytes; PDFs yield
        pypdf text (fast, offline, layout-less); .docx yields python-docx paragraphs;
        anything else falls back to a docling conversion when one is possible.
        """
        suffix = path.suffix.casefold()
        if suffix in TEXT_SUFFIXES:
            return path.read_text(encoding="utf-8")
        if suffix in PDF_SUFFIXES:
            return self._pypdf_text(path)
        if suffix in DOCX_SUFFIXES:
            return self._docx_text(path)
        # docx/doc/pptx/xlsx and other docling-supported files: no PDF layout model is
        # involved, so conversion works on an offline box from the bundled code paths.
        try:
            return self._docling_text(path)
        except Exception:
            logger.exception("docling text extraction failed for %s", path)
            raise

    # ------------------------------------------------------------------ pdf

    def _pdf_blocks(self, path: Path, document: KnowledgeDocument) -> list[KnowledgeBlock]:
        try:
            return self._docling_blocks(path, document)
        except Exception:
            # Predictable offline condition: the layout/OCR model snapshot is not in
            # the HF cache. Degrade to ordered page text rather than fail the ingest.
            logger.warning(
                "docling PDF conversion failed for %s; using pypdf plain-text fallback",
                path,
                exc_info=True,
            )
            return self._pypdf_blocks(path, document)

    def _pypdf_blocks(self, path: Path, document: KnowledgeDocument) -> list[KnowledgeBlock]:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        blocks: list[KnowledgeBlock] = []
        for page in reader.pages:
            text = (page.extract_text() or "").strip()
            if not text:
                continue
            blocks.append(
                KnowledgeBlock(
                    document_id=document.document_id,
                    order=len(blocks),
                    block_type=BlockType.PARAGRAPH,
                    content=text,
                    metadata={"parser": "pypdf", "page": len(blocks)},
                )
            )
        return blocks

    def _pypdf_text(self, path: Path) -> str:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        pages = [(page.extract_text() or "").strip() for page in reader.pages]
        return "\n\n".join(page for page in pages if page)

    # ------------------------------------------------------------------ docx

    def _docx_blocks(self, path: Path, document: KnowledgeDocument) -> list[KnowledgeBlock]:
        try:
            return self._docling_blocks(path, document)
        except Exception:
            logger.warning(
                "docling DOCX conversion failed for %s; using python-docx fallback",
                path,
                exc_info=True,
            )
            return self._docx_text_blocks(path, document)

    def _docx_text_blocks(self, path: Path, document: KnowledgeDocument) -> list[KnowledgeBlock]:
        from docx import Document

        blocks: list[KnowledgeBlock] = []
        for para in Document(str(path)).paragraphs:
            text = para.text.strip()
            if not text:
                continue
            style_name = para.style.name if para.style else None
            kind = (
                BlockType.HEADING
                if style_name and style_name.casefold().startswith("heading")
                else BlockType.PARAGRAPH
            )
            blocks.append(
                KnowledgeBlock(
                    document_id=document.document_id,
                    order=len(blocks),
                    block_type=kind,
                    content=text,
                    metadata={"parser": "python-docx"},
                )
            )
        return blocks

    def _docx_text(self, path: Path) -> str:
        from docx import Document

        return "\n\n".join(
            para.text.strip() for para in Document(str(path)).paragraphs if para.text.strip()
        )

    # ---------------------------------------------------------------- docling

    def _docling_blocks(self, path: Path, document: KnowledgeDocument) -> list[KnowledgeBlock]:
        converted = self._docling_document(path)
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

    def _docling_text(self, path: Path) -> str:
        converted = self._docling_document(path)
        return "\n\n".join(
            item.text.strip()
            for item, _ in converted.iterate_items()
            if isinstance(item, (TextItem, SectionHeaderItem)) and item.text.strip()
        )

    def _docling_document(self, path: Path):
        if self._docling is None:
            self._docling = DocumentConverter()
        return self._docling.convert(path).document


structure_parser = StructureParser()
