"""
Cache des questions : une question déjà posée réutilise son SQL validé, sans appeler le LLM.

Le coût dominant du système est la génération (environ 15 s par question sur CPU), pas l'exécution.
Clé : question normalisée (minuscules, sans accents ni ponctuation) + modèle + date du dernier relevé
(le cache est donc invalidé à chaque nouveau chargement de données).

Pas de cache sémantique (réutiliser le SQL d'une question « proche ») : mesuré avec les embeddings du
projet, « sites en panne 4G hier » et « sites en panne 5G hier » sont plus proches (cosinus 0,868) que deux
vraies reformulations (0,861). Un seuil de similarité renverrait la réponse d'une autre question.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from pathlib import Path

from netquery.schema import normalize

ROOT = Path(__file__).resolve().parents[1]
CACHE_PATH = ROOT / "data" / "cache" / "questions.json"


def question_key(question: str, model: str, data_version: str) -> str:
    q = re.sub(r"[^a-z0-9]+", " ", normalize(question)).strip()
    return hashlib.sha256(f"{q}|{model}|{data_version}".encode("utf-8")).hexdigest()


class QuestionCache:
    def __init__(self, path: Path = CACHE_PATH):
        self.path = path
        self._lock = threading.Lock()
        self._data: dict[str, dict] = (
            json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        )

    def get(self, key: str) -> str | None:
        entry = self._data.get(key)
        return entry["sql"] if entry else None

    def put(self, key: str, question: str, sql: str) -> None:
        with self._lock:
            self._data[key] = {"question": question, "sql": sql}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._data, ensure_ascii=False, indent=1), encoding="utf-8")

    def drop(self, key: str) -> None:
        with self._lock:
            if self._data.pop(key, None) is not None:
                self.path.write_text(json.dumps(self._data, ensure_ascii=False, indent=1), encoding="utf-8")

    def __len__(self) -> int:
        return len(self._data)
