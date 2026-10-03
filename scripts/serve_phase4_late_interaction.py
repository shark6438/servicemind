#!/usr/bin/env python
"""Local GPU service that turns text into BGE-M3 token-level vectors.

Run it with the interpreter that can see the cards, not the repo virtualenv::

    PYTHONPATH=src /data/shihongye/data/miniconda3/bin/python \
        scripts/serve_phase4_late_interaction.py --port 8087

The platform talks to it over loopback, the same shape as the TEI providers in
``servicemind.rag.models``. It exists because ``torch.cuda.is_available()`` is
False in the repo virtualenv (torch 2.13.0+cu130 against a 535/CUDA 12.2 driver),
so the encoder cannot live in the API process -- see the R2 plan.

Nothing is installed to make this work; the interpreter is used read-only.

Wire format
-----------
``POST /encode`` takes ``{"texts": [...], "kind": "document"|"query"}`` and
answers with an ``application/octet-stream`` body that is an ``np.savez``
archive of ``vectors`` (float32, flattened) and ``offsets`` (int64, one entry
per text plus a trailing total). Binary rather than JSON because one document
is a megabyte of floats.

Vectors travel as float32; the index writer decides the storage dtype. That
keeps the precision decision in exactly one place.
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import sys
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

from servicemind.rag.late_interaction_torch import (  # noqa: E402
    MODEL_NAME,
    BgeM3MultiVectorEncoder,
)

logger = logging.getLogger("servicemind.serve_late_interaction")

DEFAULT_SNAPSHOT = (
    "data/phase4/models/embedding/models--BAAI--bge-m3/snapshots/"
    "5617a9f61b028005a4858fdac845db406aefb181"
)
DEFAULT_REVISION = "5617a9f61b028005a4858fdac845db406aefb181"

ENCODER: BgeM3MultiVectorEncoder | None = None


def _pack(batches: list) -> bytes:
    """Serialise encoded texts as a flat float32 matrix plus offsets."""
    offsets = np.zeros(len(batches) + 1, dtype=np.int64)
    total = 0
    for position, batch in enumerate(batches):
        total += batch.tokens
        offsets[position + 1] = total
    vectors = (
        np.concatenate([batch.vectors for batch in batches], axis=0)
        if batches
        else np.zeros((0, ENCODER.dimension), dtype=np.float32)
    )
    buffer = io.BytesIO()
    np.savez(buffer, vectors=vectors.astype(np.float32), offsets=offsets)
    return buffer.getvalue()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ServiceMindLateInteraction/1"

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: A003
        logger.info("%s %s", self.address_string(), fmt % args)

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/health":
            self._send_json(404, {"error": "not found"})
            return
        assert ENCODER is not None
        import torch

        free, total = torch.cuda.mem_get_info(ENCODER.device)
        self._send_json(
            200,
            {
                "status": "ok",
                "model": MODEL_NAME,
                "revision": ENCODER.revision,
                "dimension": ENCODER.dimension,
                "device": str(ENCODER.device),
                "torch": torch.__version__,
                "cuda": torch.version.cuda,
                "gpu_free_bytes": int(free),
                "gpu_total_bytes": int(total),
                "max_doc_tokens": ENCODER.max_doc_tokens,
                "max_query_tokens": ENCODER.max_query_tokens,
                "snapshot": str(ENCODER.snapshot_dir),
            },
        )

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/encode":
            self._send_json(404, {"error": "not found"})
            return
        assert ENCODER is not None
        try:
            length = int(self.headers.get("Content-Length") or 0)
            request = json.loads(self.rfile.read(length) or b"{}")
            texts = request.get("texts")
            kind = request.get("kind", "document")
            if not isinstance(texts, list) or not all(isinstance(t, str) for t in texts):
                raise ValueError("'texts' must be a list of strings")
            if kind not in {"document", "query"}:
                raise ValueError("'kind' must be 'document' or 'query'")
            started = time.perf_counter()
            if kind == "query":
                if len(texts) != 1:
                    raise ValueError("queries are encoded one at a time")
                batches = [ENCODER.encode_query(texts[0])]
            else:
                batch_size = int(request.get("batch_size") or 16)
                batches = ENCODER.encode_documents(texts, batch_size=batch_size)
            payload = _pack(batches)
            elapsed = time.perf_counter() - started
        except Exception as error:  # noqa: BLE001 - the client must see the reason
            logger.error("encode failed: %s", error)
            traceback.print_exc()
            self._send_json(500, {"error": f"{type(error).__name__}: {error}"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Encode-Seconds", f"{elapsed:.6f}")
        self.send_header("X-Token-Count", str(sum(b.tokens for b in batches)))
        self.end_headers()
        self.wfile.write(payload)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8087)
    parser.add_argument("--snapshot", default=DEFAULT_SNAPSHOT)
    parser.add_argument("--revision", default=DEFAULT_REVISION)
    parser.add_argument("--device", default=None)
    parser.add_argument("--memory-fraction", type=float, default=0.4)
    parser.add_argument("--max-doc-tokens", type=int, default=512)
    parser.add_argument("--max-query-tokens", type=int, default=32)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    global ENCODER
    import torch

    if not torch.cuda.is_available():
        logger.error(
            "no CUDA device visible to %s (torch %s, built for CUDA %s). "
            "Refusing to fall back to CPU: the cost gate would then be measured "
            "against hardware production does not use.",
            sys.executable,
            torch.__version__,
            torch.version.cuda,
        )
        return 3
    if args.device is not None and not args.device.startswith("cuda"):
        logger.error("--device must name a CUDA device, got %s", args.device)
        return 3

    ENCODER = BgeM3MultiVectorEncoder(
        snapshot_dir=args.snapshot,
        revision=args.revision,
        device=args.device,
        memory_fraction=args.memory_fraction,
        max_doc_tokens=args.max_doc_tokens,
        max_query_tokens=args.max_query_tokens,
    )
    logger.info(
        "ready: device=%s dimension=%d revision=%s interpreter=%s",
        ENCODER.device,
        ENCODER.dimension,
        ENCODER.revision,
        sys.executable,
    )
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("interrupted; shutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
