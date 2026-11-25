"""Tests de la recherche hybride (partie BM25, sans serveur d'embeddings)."""

from netquery.retrieval import BM25, Index, tokenize


def test_tokenize():
    assert tokenize("Combien de pannes 5G chez les Opérateurs ?") == ["panne", "5g", "chez", "operateur"]


def test_bm25_classe_le_bon_document():
    docs = [tokenize(t) for t in ["sites en panne 4G", "émetteurs 5G par bande", "communes et population"]]
    scores = BM25(docs).scores(tokenize("antennes 5G par bande de fréquence"))
    assert max(range(3), key=scores.__getitem__) == 1


def test_index_exclusion():
    idx = Index(["pannes SFR", "pannes Orange", "émetteurs Free"], use_embeddings=False)
    hits = idx.search("pannes SFR", k=2, exclude={0})
    assert [h.index for h in hits][0] == 1 and all(h.index != 0 for h in hits)
