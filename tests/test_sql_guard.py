"""Tests des garde-fous SQL (n'ont pas besoin de la base pour la validation)."""

import pytest

from netquery.sql_guard import MAX_ROWS, UnsafeSQLError, validate

ALLOWED = {"fact_indispo_site", "fact_indispo", "dim_departement"}


def ok(sql, **kw):
    return validate(sql, allowed_tables=ALLOWED, **kw)


@pytest.mark.parametrize("sql", [
    "select count(*) from fact_indispo_site",
    "SELECT count(*) FROM fact_indispo_site;",
    "with x as (select * from fact_indispo) select count(*) from x",
    "select departement from fact_indispo union all select departement from dim_departement",
    "select * from fact_indispo_site f join dim_departement d using (departement) where d.nom = 'Rhône'",
    "select 1",
])
def test_select_autorise(sql):
    ok(sql)


@pytest.mark.parametrize("sql, motif", [
    ("drop table fact_indispo", "SELECT"),
    ("delete from fact_indispo_site", "SELECT"),
    ("insert into fact_indispo_site select * from fact_indispo_site", "SELECT"),
    ("update fact_indispo_site set raison = 'x'", "SELECT"),
    ("create table t as select 1", "SELECT"),
    ("select 1; drop table fact_indispo", "une seule"),
    ("copy fact_indispo to 'x.csv'", None),
    ("attach 'autre.duckdb'", None),
    ("pragma database_list", None),
    ("select * from raw_sites_indispo", "non autorisée"),
    ("select * from stg_sites_indispo", "non autorisée"),
    ("select * from information_schema.tables", "qualifiée"),
    ("select * from read_csv('C:/Windows/win.ini')", "fonction de table"),
    ("select * from read_parquet('data/lake/**/*.parquet')", "fonction de table"),
    ("select * from fact_indispo where site_id in (select site_id from raw_sites_indispo)", "non autorisée"),
    ("", "vide"),
    ("ceci n'est pas du sql", "invalide"),
])
def test_requete_refusee(sql, motif):
    with pytest.raises(UnsafeSQLError, match=motif):
        ok(sql)


def test_limit_ajoute():
    c = ok("select * from fact_indispo")
    assert c.limit_added and f"LIMIT {MAX_ROWS}" in c.sql.upper()


def test_limit_existant_conserve():
    c = ok("select * from fact_indispo limit 5")
    assert not c.limit_added and "LIMIT 5" in c.sql.upper()


def test_limit_trop_grand_abaisse():
    c = ok("select * from fact_indispo limit 999999", max_rows=100)
    assert c.limit_added and "LIMIT 100" in c.sql.upper() and "999999" not in c.sql


def test_limit_sur_union():
    c = ok("select departement from fact_indispo union select departement from dim_departement", max_rows=10)
    assert "LIMIT 10" in c.sql.upper()


def test_tables_utilisees():
    c = ok("with x as (select * from fact_indispo) select * from x join dim_departement using (departement)")
    assert c.tables == ["dim_departement", "fact_indispo"]
