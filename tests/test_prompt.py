"""Tests du choix des tables et de la reconnaissance des lieux. Requièrent la base locale."""

import pytest

from netquery.prompt import build_prompt, resolve_places, select_tables
from netquery.sql_guard import DB_PATH

pytestmark = pytest.mark.skipif(not DB_PATH.exists(), reason="base locale absente")


@pytest.mark.parametrize("question, attendu", [
    ("Combien de sites Orange en panne hier dans le Nord ?", [("departement", "59")]),
    ("Pannes en Auvergne-Rhône-Alpes", [("region", "Auvergne-Rhône-Alpes")]),
    ("sites dans les bouches du rhone", [("departement", "13")]),       # sans accents ni tirets
    ("Bouches-du-Rhône et Rhône", [("departement", "13"), ("departement", "69")]),
    ("la somme des sites en panne", []),                                # mot courant en minuscules
    ("pannes dans la Somme", [("departement", "80")]),
    ("Corse-du-Sud", [("departement", "2A")]),
])
def test_resolve_places(question, attendu):
    assert [(p.kind, p.code) for p in resolve_places(question)] == attendu


@pytest.mark.parametrize("question, tables", [
    ("Combien de sites SFR en panne 4G dans le Rhône ?", ["fact_indispo", "dim_departement"]),
    ("Combien de sites 5G mis en service par trimestre ?", ["fact_station", "dim_date"]),
    ("Quelles communes ont des sites hors service depuis plus de 7 jours ?",
     ["fact_indispo_site", "dim_commune"]),
    ("Combien d'antennes 4G en bande 800 chez Bouygues ?", ["fact_station"]),
])
def test_select_tables(question, tables):
    assert select_tables(question) == tables


def test_departement_reconnu_sans_dimension():
    pr = build_prompt("Combien de sites Orange en panne hier dans le Nord ?")
    assert pr.tables == ["fact_indispo_site"]
    assert "departement = '59'" in pr.messages[-1]["content"]


def test_question_sur_le_parc():
    # ni panne, ni déploiement, ni date : le parc de sites
    assert select_tables("Combien de sites Free en Guadeloupe ?") == ["dim_site"]


def test_operateur_outre_mer():
    pr = build_prompt("Combien de sites Free en Guadeloupe ?")
    assert "'FREE_CARAIBES'" in pr.messages[-1]["content"]


def test_exemples_de_meme_structure_en_premier():
    pr = build_prompt("Combien de sites ont reçu leur première antenne 5G par opérateur chaque trimestre de 2025 ?")
    from netquery.prompt import load_examples
    by_q = {e.question: e for e in load_examples()}
    assert all(set(by_q[q].tables) == set(pr.tables) for q in pr.examples)
