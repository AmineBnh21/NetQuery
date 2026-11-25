"""Tests de la comparaison des résultats (sans base ni modèle)."""

import datetime as dt

import pandas as pd

from netquery.evaluate import same_result


def df(**cols):
    return pd.DataFrame(cols)


def test_noms_et_ordre_des_colonnes_ignores():
    gold = df(operateur_id=["SFR", "FREE"], n=[3, 5])
    pred = df(nb_sites=[5, 3], op=["FREE", "SFR"])
    assert same_result(gold, pred)


def test_colonnes_en_plus_acceptees():
    assert same_result(df(n=[11]), df(operateur_id=["SFR"], nb_sites=[11]))


def test_valeur_differente():
    assert not same_result(df(n=[11]), df(n=[12]))


def test_nombre_de_lignes_different():
    assert not same_result(df(n=[1, 2]), df(n=[1, 2, 3]))


def test_entier_et_flottant():
    assert same_result(df(n=[2075]), df(n=[2075.0]))


def test_arrondi():
    assert same_result(df(n=[45.27]), df(n=[45.3]))
    assert not same_result(df(n=[45.27]), df(n=[0.45]))


def test_date_et_timestamp():
    assert same_result(df(d=[dt.date(2026, 11, 3)]), df(d=[pd.Timestamp("2026-11-03")]))


def test_cles_seulement():
    gold = df(operateur_id=["ORANGE"], n=[2075])
    assert same_result(gold, df(operateur_id=["ORANGE"]), keys=["operateur_id"])
    assert not same_result(gold, df(operateur_id=["ORANGE"]))


def test_colonne_reutilisee_interdite():
    # deux colonnes de référence identiques ne peuvent pas pointer vers la même colonne prédite
    assert not same_result(df(a=[1], b=[1]), df(x=[1]))
