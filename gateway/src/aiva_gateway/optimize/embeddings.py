"""Text embeddings and cross-encoder reranking for retrieval and the semantic cache (PLAN §2).

Production runs the pinned BAAI models in-process on CPU with ONNX Runtime (Space 2), downloaded from the
Hub at the pinned revision on first use. No text leaves the gateway for embedding or reranking.
`HashingEmbedder` and `LexicalReranker` are deterministic, dependency-free stand-ins for tests and offline
development; their identifiers make the backend visible wherever results are recorded.
"""

import asyncio
import hashlib
import itertools
import math
import re
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np
from numpy.typing import NDArray

EMBEDDING_DIMENSIONS = 768
QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "

Vector = NDArray[np.float32]


@dataclass(frozen=True)
class OnnxModelPin:
    repo_id: str
    revision: str
    model_file: str = "onnx/model.onnx"
    tokenizer_file: str = "tokenizer.json"
    max_length: int = 512


BGE_BASE = OnnxModelPin("BAAI/bge-base-en-v1.5", "a5beb1e3e68b9ab74eb54cfd186867f64f240e1a")
BGE_RERANKER = OnnxModelPin("BAAI/bge-reranker-base", "2cfc18c9415c912f9d8155881c133215df768a70")


class Embedder(Protocol):
    model_id: str
    dimensions: int

    def embed(self, texts: Sequence[str], *, query: bool = False) -> Vector:
        """Return L2-normalised rows, one per text."""
        ...


class Reranker(Protocol):
    model_id: str
    # Passages scoring below this are dropped as irrelevant (provisional; calibrated in P2 benchmarks).
    min_relevance: float

    def score(self, query: str, passages: Sequence[str]) -> list[float]: ...


async def embed_async(embedder: Embedder, texts: Sequence[str], *, query: bool = False) -> Vector:
    return await asyncio.to_thread(embedder.embed, texts, query=query)


def _normalise(matrix: NDArray[Any]) -> Vector:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return np.asarray(matrix / norms, dtype=np.float32)


_WORD = re.compile(r"[a-z0-9]+(?:[.-][a-z0-9]+)*")


_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "how",
        "i",
        "in",
        "is",
        "it",
        "my",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "was",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "with",
        "you",
        "your",
    }
)


def tokens(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def terms(text: str) -> list[str]:
    """Tokens without stopwords, for lexical scoring."""
    return [t for t in tokens(text) if t not in _STOPWORDS]


class HashingEmbedder:
    """Signed feature hashing of unigrams and bigrams. Deterministic; not a semantic model."""

    model_id = "hashing-768-v1"
    dimensions = EMBEDDING_DIMENSIONS

    def embed(self, texts: Sequence[str], *, query: bool = False) -> Vector:
        matrix = np.zeros((len(texts), self.dimensions), dtype=np.float32)
        for row, text in enumerate(texts):
            words = tokens(text)
            features = words + [f"{a} {b}" for a, b in itertools.pairwise(words)]
            for feature in features:
                digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
                index = int.from_bytes(digest[:4], "little") % self.dimensions
                sign = 1.0 if digest[4] & 1 else -1.0
                matrix[row, index] += sign
        return _normalise(matrix)


class LexicalReranker:
    """Query-term coverage with length damping. A stand-in when the cross-encoder is not loaded."""

    model_id = "lexical-v1"
    min_relevance = 0.0

    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        query_terms = set(terms(query))
        scores = []
        for passage in passages:
            words = terms(passage)
            if not query_terms or not words:
                scores.append(0.0)
                continue
            hits = sum(1 for word in set(words) if word in query_terms)
            scores.append(hits / len(query_terms) - 0.01 * math.log1p(len(words)))
        return scores


class _OnnxModel:
    """Lazily downloads and loads a pinned ONNX model and its tokenizer (thread-safe)."""

    def __init__(self, pin: OnnxModelPin) -> None:
        self.pin = pin
        self._lock = threading.Lock()
        self._session: Any = None
        self._tokenizer: Any = None
        self._input_names: set[str] = set()

    def load(self) -> tuple[Any, Any]:
        with self._lock:
            if self._session is None:
                import onnxruntime
                from huggingface_hub import hf_hub_download
                from tokenizers import Tokenizer

                model_path = hf_hub_download(
                    self.pin.repo_id, self.pin.model_file, revision=self.pin.revision
                )
                tokenizer_path = hf_hub_download(
                    self.pin.repo_id, self.pin.tokenizer_file, revision=self.pin.revision
                )
                tokenizer = Tokenizer.from_file(tokenizer_path)
                tokenizer.enable_truncation(max_length=self.pin.max_length)
                tokenizer.enable_padding()
                options = onnxruntime.SessionOptions()
                options.intra_op_num_threads = 2
                session = onnxruntime.InferenceSession(
                    model_path, sess_options=options, providers=["CPUExecutionProvider"]
                )
                self._input_names = {i.name for i in session.get_inputs()}
                self._session, self._tokenizer = session, tokenizer
            return self._session, self._tokenizer

    def feeds(self, encodings: list[Any]) -> dict[str, NDArray[np.int64]]:
        feeds = {
            "input_ids": np.array([e.ids for e in encodings], dtype=np.int64),
            "attention_mask": np.array([e.attention_mask for e in encodings], dtype=np.int64),
        }
        if "token_type_ids" in self._input_names:
            feeds["token_type_ids"] = np.array([e.type_ids for e in encodings], dtype=np.int64)
        return feeds


class OnnxEmbedder:
    """BAAI/bge-base-en-v1.5 (CLS pooling, normalised), pinned by revision."""

    dimensions = EMBEDDING_DIMENSIONS

    def __init__(self, pin: OnnxModelPin = BGE_BASE, batch_size: int = 16) -> None:
        self._model = _OnnxModel(pin)
        self.model_id = f"{pin.repo_id}@{pin.revision[:12]}"
        self.batch_size = batch_size

    def embed(self, texts: Sequence[str], *, query: bool = False) -> Vector:
        session, tokenizer = self._model.load()
        prepared = [f"{QUERY_INSTRUCTION}{t}" if query else t for t in texts]
        rows: list[NDArray[Any]] = []
        for start in range(0, len(prepared), self.batch_size):
            encodings = tokenizer.encode_batch(prepared[start : start + self.batch_size])
            hidden = session.run(None, self._model.feeds(encodings))[0]
            rows.append(np.asarray(hidden)[:, 0, :])
        if not rows:
            return np.zeros((0, self.dimensions), dtype=np.float32)
        return _normalise(np.vstack(rows))


class OnnxCrossEncoder:
    """BAAI/bge-reranker-base relevance logits, pinned by revision."""

    min_relevance = -4.0

    def __init__(self, pin: OnnxModelPin = BGE_RERANKER, batch_size: int = 8) -> None:
        self._model = _OnnxModel(pin)
        self.model_id = f"{pin.repo_id}@{pin.revision[:12]}"
        self.batch_size = batch_size

    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        session, tokenizer = self._model.load()
        scores: list[float] = []
        for start in range(0, len(passages), self.batch_size):
            batch = [(query, p) for p in passages[start : start + self.batch_size]]
            encodings = tokenizer.encode_batch(batch)
            logits = np.asarray(session.run(None, self._model.feeds(encodings))[0])
            scores.extend(float(v) for v in logits.reshape(len(batch), -1)[:, 0])
        return scores


def build_embedder(backend: str) -> Embedder:
    if backend == "onnx":
        return OnnxEmbedder()
    if backend == "hashing":
        return HashingEmbedder()
    raise ValueError(f"unknown embedding backend {backend!r}")


def build_reranker(backend: str) -> Reranker:
    if backend == "onnx":
        return OnnxCrossEncoder()
    if backend == "lexical":
        return LexicalReranker()
    raise ValueError(f"unknown reranker backend {backend!r}")
