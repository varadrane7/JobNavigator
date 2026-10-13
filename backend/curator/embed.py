"""Résumé-vs-JD similarity with a local embedding model (LM Studio's /v1/embeddings).

Cheap pre-filter: a few hundred JDs embed in seconds, so only the most similar slice
of the day's pool goes through LLM scoring.
"""
import logging
import math

import httpx

from backend.analyzer.llm_client import _get_setting
from backend.models.db import SessionLocal

logger = logging.getLogger("jobnavigator.curator.embed")

_BATCH = 16


def _config() -> tuple[str, str]:
    db = SessionLocal()
    try:
        return (_get_setting(db, "llm_local_base_url", "http://localhost:1234/v1").rstrip("/"),
                _get_setting(db, "curate_embed_model", ""))
    finally:
        db.close()


async def embed(texts: list[str], kind: str = "document") -> list[list[float]] | None:
    """Vectors for `texts` (nomic wants a task prefix), or None when no model is set or the call fails."""
    base, model = _config()
    if not model or not texts:
        return None
    prefix = "search_query: " if kind == "query" else "search_document: "
    out = []
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            for i in range(0, len(texts), _BATCH):
                chunk = [prefix + (t or "")[:6000] for t in texts[i:i + _BATCH]]
                resp = await client.post(f"{base}/embeddings", json={"model": model, "input": chunk})
                resp.raise_for_status()
                data = sorted(resp.json()["data"], key=lambda d: d["index"])
                out.extend(d["embedding"] for d in data)
    except Exception as e:
        logger.warning(f"embedding failed, curator falls back to LLM scores only: {e}")
        return None
    return out


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


async def similarity(resumes: list[str], jds: list[str]) -> list[float] | None:
    """Per JD, the best cosine similarity against any résumé."""
    rv = await embed(resumes, "query")
    if not rv:
        return None
    jv = await embed(jds, "document")
    if not jv:
        return None
    return [max(cosine(r, j) for r in rv) for j in jv]


def percentiles(values: list[float]) -> list[float]:
    """Rank of each value in the list as 0..1 (best = 1)."""
    if not values:
        return []
    order = sorted(range(len(values)), key=lambda i: values[i])
    out = [0.0] * len(values)
    n = max(1, len(values) - 1)
    for rank, i in enumerate(order):
        out[i] = rank / n
    return out
