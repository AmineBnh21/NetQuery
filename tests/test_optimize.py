"""Tests de la réécriture « dernier relevé » (sans base : date fournie)."""

import datetime as dt

from netquery.optimize import rewrite_latest_date
from netquery.sql_guard import validate

ALLOWED = {"fact_indispo_site", "fact_indispo", "dim_departement"}
DAY = dt.date(2026, 11, 24)


def rw(sql):
    return rewrite_latest_date(validate(sql, allowed_tables=ALLOWED), latest=DAY)


def test_sous_requete_remplacee():
    c, n = rw("select count(*) from fact_indispo_site where date = (select max(date) from fact_indispo_site)")
    assert n == 1 and "2026-11-24" in c.sql and "MAX" not in c.sql.upper()


def test_sous_requete_avec_intervalle():
    c, n = rw("select count(*) from fact_indispo where date > (select max(date) from fact_indispo) - interval 7 day")
    assert n == 1 and "2026-11-24" in c.sql


def test_sous_requete_filtree_conservee():
    # max(date) d'un sous-ensemble : ce n'est pas le dernier relevé global
    c, n = rw("select count(*) from fact_indispo_site where date = "
              "(select max(date) from fact_indispo_site where operateur_id = 'SFR')")
    assert n == 0


def test_autre_colonne_conservee():
    c, n = rw("select count(*) from fact_indispo_site where debut = (select max(debut) from fact_indispo_site)")
    assert n == 0
