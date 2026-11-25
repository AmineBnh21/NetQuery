"""
Client LLM générique : toute API compatible OpenAI (Ollama en local par défaut).

Configuration par variables d'environnement (fichier .env, ignoré par git) :
  LLM_BASE_URL   défaut http://localhost:11434/v1 (Ollama)
  LLM_MODEL      défaut qwen2.5-coder:3b
  LLM_API_KEY    inutile pour Ollama ; à renseigner pour un fournisseur cloud
  LLM_NUM_CTX    taille du contexte demandée à Ollama (défaut 4096)
  LLM_TIMEOUT    délai maximal d'une réponse en secondes (défaut 120)

Le reste du code n'utilise que la classe LLM et sa méthode complete() : changer de modèle
ou de fournisseur ne demande que de modifier le .env.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Protocol

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Completion:
    text: str
    model: str
    latency_s: float
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class ChatModel(Protocol):
    """Interface minimale attendue par le pipeline (permet un faux LLM dans les tests)."""

    def complete(self, messages: list[dict], max_tokens: int = 512) -> Completion: ...


class LLM:
    def __init__(self, base_url: str | None = None, model: str | None = None,
                 api_key: str | None = None, num_ctx: int | None = None, timeout: float | None = None):
        from openai import OpenAI  # import ici : les tests avec un faux LLM n'en ont pas besoin

        self.base_url = base_url or os.getenv("LLM_BASE_URL", "http://localhost:11434/v1")
        self.model = model or os.getenv("LLM_MODEL", "qwen2.5-coder:3b")
        self.num_ctx = num_ctx or int(os.getenv("LLM_NUM_CTX", "4096"))
        self.client = OpenAI(
            base_url=self.base_url,
            # Ollama ignore la clé mais le SDK en exige une
            api_key=api_key or os.getenv("LLM_API_KEY") or "ollama",
            timeout=timeout or float(os.getenv("LLM_TIMEOUT", "120")),
            max_retries=1,
        )

    @property
    def is_ollama(self) -> bool:
        return ":11434" in self.base_url or "ollama" in self.base_url.lower()

    def complete(self, messages: list[dict], max_tokens: int = 512) -> Completion:
        extra = {"options": {"num_ctx": self.num_ctx}} if self.is_ollama else None
        t0 = time.perf_counter()
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=0,
            max_tokens=max_tokens,
            extra_body=extra,
        )
        usage = resp.usage
        return Completion(
            text=resp.choices[0].message.content or "",
            model=self.model,
            latency_s=time.perf_counter() - t0,
            prompt_tokens=getattr(usage, "prompt_tokens", None),
            completion_tokens=getattr(usage, "completion_tokens", None),
        )
