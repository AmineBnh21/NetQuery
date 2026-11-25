import os
import sys
from pathlib import Path

# rend le paquet netquery importable sans installation
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# tests reproductibles et sans serveur : recherche BM25 seule (pas d'embeddings)
os.environ.setdefault("EMBED_MODEL", "off")
