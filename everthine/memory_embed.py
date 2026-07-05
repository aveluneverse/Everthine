"""Local sentence-embedding engine for long-term memory.

Runs entirely on this machine: no API calls, no tokens, no Claude quota.
The model downloads once from Hugging Face on first use (the setup guide
pre-downloads it so the first chat never stalls); afterwards it loads
from the local cache. Tests never import the real model -- they inject a
fake through set_embed_fn().
"""
from __future__ import annotations

import threading

_embed_fn = None            # injected seam; None = use the real model
_model = None
_lock = threading.Lock()


def set_embed_fn(fn) -> None:
    """Test seam: replace the embedding backend (None restores the model)."""
    global _embed_fn
    _embed_fn = fn


def _get_model(model_name: str):
    global _model
    if _model is None:
        with _lock:
            if _model is None:
                from sentence_transformers import SentenceTransformer
                _model = SentenceTransformer(model_name)
    return _model


def embed_texts(texts: list[str], model_name: str) -> list[list[float]]:
    """Batch-encode to L2-normalized vectors (cosine == dot product)."""
    if _embed_fn is not None:
        return [_embed_fn(t) for t in texts]
    model = _get_model(model_name)
    vecs = model.encode(texts, convert_to_numpy=True, show_progress_bar=False,
                        batch_size=32, normalize_embeddings=True)
    return [v.tolist() for v in vecs]
