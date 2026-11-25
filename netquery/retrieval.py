"""
Recherche des exemples et des tables les plus proches d'une question (RAG).

Recherche hybride :
  - BM25 sur les mots (sans modèle, robuste aux codes et noms propres : '5G', 'SFR', 'Rhône') ;
  - similarité cosinus d'embeddings (sens de la phrase : « coupure » ~ « panne ») ;
  - fusion des deux classements par Reciprocal Rank Fusion (RRF), qui ne demande pas de normaliser les scores.

Les embeddings viennent d'une API compatible OpenAI (Ollama par défaut, modèle multilingue)
et sont mis en cache sur disque. Sans serveur d'embeddings, seule la recherche BM25 est utilisée.

Variables d'environnement : EMBED_BASE_URL (défaut LLM_BASE_URL), EMBED_MODEL (défaut paraphrase-multilingual).
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from netquery.schema import load, normalize

ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = ROOT / "data" / "cache"
RRF_K = 60  # constante usuelle de la RRF

log = logging.getLogger(__name__)

STOPWORDS = set("""
a au aux avec ce ces cette d de des du elle en est et il ils je l la le les leur leurs lui ma mais me
mes mon ne nos notre nous on ou par pas pour qu que qui sa se ses son sur ta te tes ton tu un une vos
votre vous y combien quel quelle quels quelles est-ce sont ont ete etaient etait avoir etre plus moins
""".split())


def tokenize(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", normalize(text))
    out = []
    for w in words:
        if w in STOPWORDS or len(w) < 2 and not w.isdigit():
            continue
        # racinisation minimale : pluriels (pannes -> panne, operateurs -> operateur)
        if len(w) > 3 and w[-1] in "sx" and not w[-2].isdigit():
            w = w[:-1]
        out.append(w)
    return out


# --------------------------------------------------------------------------- BM25

class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.docs, self.k1, self.b = docs, k1, b
        self.avgdl = sum(map(len, docs)) / max(len(docs), 1)
        df = Counter(t for d in docs for t in set(d))
        n = len(docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}
        self.tf = [Counter(d) for d in docs]

    def scores(self, query: list[str]) -> list[float]:
        out = []
        for d, tf in zip(self.docs, self.tf):
            s = 0.0
            for t in query:
                if t not in tf:
                    continue
                f = tf[t]
                s += self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * len(d) / self.avgdl))
            out.append(s)
        return out


# --------------------------------------------------------------------------- embeddings

class Embedder:
    def __init__(self, base_url: str | None = None, model: str | None = None):
        from openai import OpenAI

        self.model = model or os.getenv("EMBED_MODEL", "paraphrase-multilingual")
        base_url = base_url or os.getenv("EMBED_BASE_URL") or os.getenv("LLM_BASE_URL", "http://localhost:11434/v1")
        self.client = OpenAI(base_url=base_url, api_key=os.getenv("LLM_API_KEY") or "ollama",
                             timeout=30, max_retries=0)
        self.cache_path = CACHE_DIR / f"embeddings_{re.sub(r'[^a-zA-Z0-9]+', '_', self.model)}.json"
        self.cache: dict[str, list[float]] = (
            json.loads(self.cache_path.read_text(encoding="utf-8")) if self.cache_path.exists() else {}
        )

    @staticmethod
    def _key(text: str) -> str:
        return hashlib.sha1(text.encode("utf-8")).hexdigest()

    def embed(self, texts: list[str]) -> list[list[float]]:
        missing = [t for t in dict.fromkeys(texts) if self._key(t) not in self.cache]
        if missing:
            resp = self.client.embeddings.create(model=self.model, input=missing)
            for t, item in zip(missing, resp.data):
                self.cache[self._key(t)] = item.embedding
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(self.cache), encoding="utf-8")
        return [self.cache[self._key(t)] for t in texts]


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


@lru_cache(maxsize=1)
def get_embedder() -> Embedder | None:
    if os.getenv("EMBED_MODEL", "x").lower() in ("", "none", "off"):
        return None
    try:
        emb = Embedder()
        emb.embed(["test"])  # vérifie que le serveur et le modèle répondent
        return emb
    except Exception as e:  # noqa: BLE001 - serveur absent : on garde BM25 seul
        log.warning("embeddings indisponibles (%s) : recherche BM25 seule", e)
        return None


# --------------------------------------------------------------------------- index

@dataclass
class Hit:
    index: int
    score: float      # score RRF
    bm25: float
    cosine: float | None


class Index:
    def __init__(self, texts: list[str], use_embeddings: bool = True):
        self.texts = texts
        self.bm25 = BM25([tokenize(t) for t in texts])
        self.embedder = get_embedder() if use_embeddings else None
        self.vectors = self.embedder.embed(texts) if self.embedder else None

    def search(self, query: str, k: int = 3, exclude: set[int] | None = None) -> list[Hit]:
        exclude = exclude or set()
        ids = [i for i in range(len(self.texts)) if i not in exclude]
        bm = self.bm25.scores(tokenize(query))
        rankings = [sorted(ids, key=lambda i: -bm[i])]
        cos = None
        if self.embedder:
            qv = self.embedder.embed([query])[0]
            cos = [cosine(qv, v) for v in self.vectors]
            rankings.append(sorted(ids, key=lambda i: -cos[i]))
        rrf = {i: 0.0 for i in ids}
        for ranking in rankings:
            for rank, i in enumerate(ranking):
                rrf[i] += 1 / (RRF_K + rank + 1)
        best = sorted(ids, key=lambda i: -rrf[i])[:k]
        return [Hit(i, rrf[i], bm[i], cos[i] if cos else None) for i in best]


def table_text(name: str) -> str:
    t = load().tables[name]
    cols = " ; ".join(c.desc for c in t.columns)
    return f"{name} : {t.description} {t.usage or ''} {cols}"


@lru_cache(maxsize=2)
def example_index(use_embeddings: bool = True) -> Index:
    from netquery.prompt import load_examples
    return Index([e.question for e in load_examples()], use_embeddings)


@lru_cache(maxsize=2)
def table_index(use_embeddings: bool = True) -> tuple[list[str], Index]:
    names = list(load().tables)
    return names, Index([table_text(n) for n in names], use_embeddings)
