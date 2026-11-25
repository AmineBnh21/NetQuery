"""Tests du pipeline avec un faux LLM (aucun modèle nécessaire). Requièrent la base locale."""

import duckdb
import pytest

from netquery.llm import Completion
from netquery.pipeline import ask, extract_sql
from netquery.sql_guard import DB_PATH, readonly_connection

pytestmark = pytest.mark.skipif(not DB_PATH.exists(), reason="base locale absente (lancer ingest + transform)")


class FakeLLM:
    """Renvoie les réponses dans l'ordre (la dernière est répétée)."""

    def __init__(self, *texts):
        self.texts = list(texts)
        self.messages = None
        self.calls = 0

    def complete(self, messages, max_tokens=512):
        self.messages = list(messages)
        text = self.texts[min(self.calls, len(self.texts) - 1)]
        self.calls += 1
        return Completion(text=text, model="fake", latency_s=0.0)


def test_extract_sql_bloc():
    assert extract_sql("Voici :\n```sql\nselect 1\n```\nVoilà") == "select 1"


def test_extract_sql_sans_bloc():
    assert extract_sql("La requête est : SELECT 1") == "SELECT 1"


def test_reponse_correcte():
    llm = FakeLLM("```sql\nselect operateur_id, count(*) as n from fact_indispo_site group by 1\n```")
    ans = ask("Combien de sites en panne par opérateur ?", llm=llm)
    assert ans.ok, ans.error
    assert set(ans.df["operateur_id"]) == {"ORANGE", "SFR", "BOUYGUES", "FREE"}
    # le prompt contient bien le schéma de la table retenue et la question
    user = llm.messages[-1]["content"]
    assert "fact_indispo_site (" in user and "Combien de sites en panne" in user


def test_requete_dangereuse_refusee():
    ans = ask("Supprime les sites en panne", llm=FakeLLM("```sql\ndrop table fact_indispo_site\n```"))
    assert ans.error_kind == "securite"
    con = readonly_connection()
    assert con.execute("select count(*) from fact_indispo_site").fetchone()[0] > 0
    con.close()


def test_lecture_fichier_refusee():
    ans = ask("Lis le fichier des sites en panne", llm=FakeLLM("select * from read_csv('data/schema_profile.md')"))
    assert ans.error_kind == "securite"


def test_erreur_execution():
    ans = ask("Combien de sites en panne ?", llm=FakeLLM("select colonne_inexistante from fact_indispo_site"))
    assert ans.error_kind == "execution"


def test_pas_de_sql():
    ans = ask("Combien de sites en panne ?", llm=FakeLLM("Je ne sais pas."))
    assert ans.error_kind == "extraction"


def test_auto_correction():
    llm = FakeLLM("select nb from fact_indispo_site", "select count(*) as nb from fact_indispo_site")
    ans = ask("Combien de sites en panne ?", llm=llm)
    assert ans.ok and ans.nb_corrections == 1
    # le modèle a reçu sa réponse fautive et le message d'erreur de DuckDB
    assert llm.messages[-2]["role"] == "assistant"
    assert "erreur d'exécution" in llm.messages[-1]["content"]


def test_explication_colonne_mal_prefixee():
    sql = "select d.operateur_id from fact_indispo_site f join dim_departement d using (departement)"
    llm = FakeLLM(sql, "select f.operateur_id from fact_indispo_site f join dim_departement d using (departement)")
    ans = ask("Combien de sites en panne ?", llm=llm)
    assert ans.ok
    assert "operateur_id se trouve dans : fact_indispo_site (alias f)" in llm.messages[-1]["content"]


def test_auto_correction_limitee():
    llm = FakeLLM("select nb from fact_indispo_site")
    ans = ask("Combien de sites en panne ?", llm=llm, max_retries=2)
    assert not ans.ok and llm.calls == 3 and ans.nb_corrections == 2


def test_connexion_lecture_seule():
    con = readonly_connection()
    with pytest.raises(duckdb.Error):
        con.execute("create table t (x int)")
    # accès aux fichiers coupé, même si une requête passait la validation
    with pytest.raises(duckdb.Error):
        con.execute("select * from read_csv('data/schema_profile.md')")
    # configuration verrouillée : impossible de réactiver l'accès externe
    with pytest.raises(duckdb.Error):
        con.execute("set enable_external_access = true")
    con.close()


def test_reecriture_dernier_releve():
    llm = FakeLLM("select count(*) as n from fact_indispo_site where date = (select max(date) from fact_indispo_site)")
    ans = ask("Combien de sites en panne hier ?", llm=llm)
    assert ans.ok and ans.cost["reecritures"] == 1
    assert "max(" not in ans.sql.lower() and "CAST('" in ans.sql


def test_cache_des_questions(tmp_path):
    from netquery.cache import QuestionCache

    cache = QuestionCache(tmp_path / "q.json")
    llm = FakeLLM("select count(*) as n from fact_indispo_site")
    a1 = ask("Combien de sites en panne ?", llm=llm, cache=cache)
    # même question, casse, accents et ponctuation différents : pas de nouvel appel au modèle
    a2 = ask("combien de SITES en panne", llm=llm, cache=cache)
    assert a1.ok and a2.ok and a2.from_cache and llm.calls == 1
    assert a1.df.iloc[0, 0] == a2.df.iloc[0, 0]
    # question différente : nouvel appel
    ask("Combien de sites SFR en panne ?", llm=llm, cache=cache)
    assert llm.calls == 2


def test_question_de_suivi():
    from netquery.prompt import Turn

    prev = Turn("Combien de sites SFR en panne hier dans le Rhône ?",
                "select count(*) as n from fact_indispo_site where operateur_id = 'SFR' and departement = '69'")
    llm = FakeLLM("select count(*) as n from fact_indispo_site where operateur_id = 'BOUYGUES' and departement = '69'")
    ans = ask("Et pour Bouygues Telecom ?", llm=llm, previous=prev)
    assert ans.ok and ans.followup
    user = llm.messages[-1]["content"]
    assert "Question précédente" in user and "operateur_id = 'SFR'" in user
    assert ans.warnings == []          # département repris de la question précédente : pas d'alerte


def test_filtre_non_demande_signale():
    llm = FakeLLM("select count(*) as n from fact_indispo_site where operateur_id = 'SFR'")
    ans = ask("Combien de sites en panne ?", llm=llm)
    assert ans.ok and ans.warnings == ["opérateur SFR"] and "opérateur : SFR" in ans.filters


def test_question_vague_demande_precision():
    llm = FakeLLM("select 1")
    ans = ask("Bonjour", llm=llm)
    assert ans.error_kind == "clarification" and llm.calls == 0
