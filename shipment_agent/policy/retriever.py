"""Policy retriever with metadata filtering.

Uses a small built-in BM25 so it runs with no extra dependencies. The
interface (search(query, carrier=, topic=, k=)) is what the tools depend on,
so this can later be replaced by a LangChain vector store retriever,
e.g. Chroma or FAISS with embeddings, filtering on the same metadata.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from pathlib import Path

from ..schemas import PolicyChunk

POLICY_FILE = Path(__file__).with_name("policies.md")
_META = re.compile(r"<!--(.*?)-->", re.S)
_TOKEN = re.compile(r"[a-z0-9₹,]+")
_STOP = set("a an the of to and or for is are be it in on at by with as this that if after not any".split())


def _tokens(text: str) -> list[str]:
    toks = [t.strip(",") for t in _TOKEN.findall(text.lower())]
    return [t.rstrip("s") if len(t) > 3 else t for t in toks if t and t not in _STOP]


def load_chunks(path: Path = POLICY_FILE) -> list[PolicyChunk]:
    chunks = []
    for section in re.split(r"^# ", path.read_text(encoding="utf-8"), flags=re.M):
        section = section.strip()
        if not section:
            continue
        title, _, body = section.partition("\n")
        meta = {}
        m = _META.search(body)
        if m:
            for pair in m.group(1).split(";"):
                k, _, v = pair.partition(":")
                meta[k.strip()] = v.strip()
            body = _META.sub("", body)
        chunks.append(PolicyChunk(chunk_id=meta.pop("id"), title=title.strip(), text=body.strip(), metadata=meta))
    return chunks


class PolicyRetriever:
    def __init__(self, chunks: list[PolicyChunk] | None = None, k1: float = 1.5, b: float = 0.75):
        self.chunks = chunks or load_chunks()
        self.k1, self.b = k1, b
        self._docs = [_tokens(c.title + " " + c.text + " " + " ".join(c.metadata.values())) for c in self.chunks]
        self._tf = [Counter(d) for d in self._docs]
        self._avgdl = sum(map(len, self._docs)) / len(self._docs)
        df = Counter(t for d in self._docs for t in set(d))
        n = len(self._docs)
        self._idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def _score(self, q: list[str], i: int) -> float:
        tf, dl = self._tf[i], len(self._docs[i])
        s = 0.0
        for t in q:
            if t in tf:
                s += self._idf[t] * tf[t] * (self.k1 + 1) / (tf[t] + self.k1 * (1 - self.b + self.b * dl / self._avgdl))
        return s

    def search(self, query: str, carrier: str | None = None, topic: str | None = None, k: int = 3) -> list[PolicyChunk]:
        q = _tokens(query)
        results = []
        for i, c in enumerate(self.chunks):
            if carrier and c.metadata.get("carrier") not in (carrier, "all"):
                continue
            if topic and c.metadata.get("topic") != topic:
                continue
            score = self._score(q, i)
            if score > 0:
                results.append(c.model_copy(update={"score": round(score, 3)}))
        return sorted(results, key=lambda c: c.score, reverse=True)[:k]
