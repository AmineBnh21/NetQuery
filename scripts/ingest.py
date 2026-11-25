"""
Ingestion des données réseau mobile (Arcep + ANFR) dans DuckDB.

Sources :
  - Arcep "Sites indisponibles" : un GeoJSON par jour (sites HS déclarés par les opérateurs)
  - ANFR "Données sur les réseaux mobiles" : archive ZIP hebdomadaire (stations 2G/3G/4G/5G)
  - geo.api.gouv.fr : référentiel communes / départements / régions (noms, population)

Les fichiers bruts sont mis en cache dans data/raw/ (rien n'est retéléchargé deux fois).
Les sites indisponibles sont stockés en Parquet partitionné par date (data/lake/indispo/date=YYYY-MM-DD/),
ce qui prépare le terrain pour l'optimisation des coûts (filtre sur la partition).

Usage :
  python scripts/ingest.py indispo --days 30
  python scripts/ingest.py indispo --start 2026-10-01 --end 2026-10-31
  python scripts/ingest.py anfr
  python scripts/ingest.py geo
  python scripts/ingest.py all --days 30
  python scripts/ingest.py profile
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import re
import sys
import zipfile
from pathlib import Path

import duckdb
import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# --------------------------------------------------------------------------- config

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
LAKE_DIR = DATA_DIR / "lake"
DB_PATH = DATA_DIR / "netquery.duckdb"
PROFILE_PATH = DATA_DIR / "schema_profile.md"

DATAGOUV_API = "https://www.data.gouv.fr/api/1/datasets/{slug}/"
INDISPO_URL = "https://arcep.s3.rbx.io.cloud.ovh.net/sites-indisponibles/all/{d}/raw{d}.geojson"
ANFR_SLUG = "donnees-sur-les-reseaux-mobiles"
GEO_API = "https://geo.api.gouv.fr"
GEO_ENDPOINTS = {
    # table brute -> chemin de l'API
    "raw_geo_communes": "/communes?fields=nom,code,codeDepartement,codeRegion,population&format=json",
    "raw_geo_arrondissements": "/communes?type=arrondissement-municipal"
                               "&fields=nom,code,codeDepartement,codeRegion,population,codeParent&format=json",
    "raw_geo_departements": "/departements",
    "raw_geo_regions": "/regions",
}

log = logging.getLogger("ingest")


# --------------------------------------------------------------------------- utils

def http_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=4, backoff_factor=1.5, status_forcelist=(429, 500, 502, 503, 504))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers["User-Agent"] = "netquery-ingest/0.1 (projet etudiant)"
    return s


def download(session: requests.Session, url: str, dest: Path) -> Path | None:
    """Télécharge url vers dest si absent. Renvoie None si la ressource n'existe pas (404)."""
    if dest.exists() and dest.stat().st_size > 0:
        log.debug("cache: %s", dest.name)
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    with session.get(url, stream=True, timeout=120) as r:
        if r.status_code in (403, 404):
            log.warning("introuvable (%s): %s", r.status_code, url)
            return None
        r.raise_for_status()
        tmp = dest.with_suffix(dest.suffix + ".part")
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)
        tmp.replace(dest)
    log.info("téléchargé: %s (%.1f Mo)", dest.name, dest.stat().st_size / 1e6)
    return dest


def safe_name(name: str) -> str:
    name = re.sub(r"[^0-9a-zA-Z]+", "_", name).strip("_").lower()
    return name if not name[:1].isdigit() else f"t_{name}"


def date_range(start: dt.date, end: dt.date):
    d = start
    while d <= end:
        yield d
        d += dt.timedelta(days=1)


# --------------------------------------------------------------------------- Arcep : sites indisponibles

def geojson_to_df(path: Path, day: dt.date) -> pd.DataFrame:
    with open(path, encoding="utf-8") as f:
        gj = json.load(f)
    rows = []
    for feat in gj.get("features", []):
        props = dict(feat.get("properties") or {})
        geom = feat.get("geometry") or {}
        coords = geom.get("coordinates") or [None, None]
        if geom.get("type") == "Point" and len(coords) >= 2:
            props["lon"], props["lat"] = coords[0], coords[1]
        rows.append(props)
    df = pd.DataFrame(rows)
    # Tout en texte au stade brut : le typage se fera dans la couche "staging".
    df = df.astype("string")
    df["date_releve"] = pd.Timestamp(day)
    return df


def ingest_indispo(con: duckdb.DuckDBPyConnection, start: dt.date, end: dt.date) -> None:
    session = http_session()
    ok = 0
    for day in date_range(start, end):
        d = day.isoformat()
        part = LAKE_DIR / "indispo" / f"date={d}" / "part.parquet"
        if part.exists():
            ok += 1
            continue
        raw = download(session, INDISPO_URL.format(d=d), RAW_DIR / "indispo" / f"{d}.geojson")
        if raw is None:
            continue
        df = geojson_to_df(raw, day)
        part.parent.mkdir(parents=True, exist_ok=True)
        df.drop(columns=["date_releve"]).to_parquet(part, index=False)
        log.info("%s : %d sites indisponibles", d, len(df))
        ok += 1

    if ok == 0:
        log.error("aucun fichier de sites indisponibles chargé")
        return

    glob = (LAKE_DIR / "indispo" / "*" / "*.parquet").as_posix()
    con.execute(f"""
        CREATE OR REPLACE VIEW raw_sites_indispo AS
        SELECT * FROM read_parquet('{glob}', hive_partitioning = true, union_by_name = true)
    """)
    n, dmin, dmax = con.execute(
        "SELECT count(*), min(date), max(date) FROM raw_sites_indispo"
    ).fetchall()[0]
    log.info("vue raw_sites_indispo : %d lignes, du %s au %s", n, dmin, dmax)


# --------------------------------------------------------------------------- ANFR : stations

def latest_anfr_zip(session: requests.Session) -> tuple[str, str]:
    r = session.get(DATAGOUV_API.format(slug=ANFR_SLUG), timeout=60)
    r.raise_for_status()
    resources = [
        res for res in r.json().get("resources", [])
        if (res.get("format") or "").lower() == "zip" or res.get("url", "").lower().endswith(".zip")
    ]
    if not resources:
        raise RuntimeError("aucune archive ZIP trouvée sur le jeu de données ANFR")
    resources.sort(key=lambda res: res.get("last_modified") or res.get("created_at") or "", reverse=True)
    best = resources[0]
    return best["url"], best.get("title") or Path(best["url"]).name


def load_csv(con: duckdb.DuckDBPyConnection, path: Path, table: str) -> None:
    p = path.as_posix()
    for enc in ("utf-8", "latin-1"):
        try:
            con.execute(f"""
                CREATE OR REPLACE TABLE {table} AS
                SELECT * FROM read_csv('{p}', all_varchar = true, header = true,
                                       sample_size = -1, normalize_names = true,
                                       encoding = '{enc}', ignore_errors = false)
            """)
            n = con.execute(f"SELECT count(*) FROM {table}").fetchall()[0][0]
            log.info("table %s : %d lignes (encodage %s)", table, n, enc)
            return
        except duckdb.Error as e:
            log.debug("échec %s avec %s : %s", path.name, enc, e)
    log.error("impossible de lire %s", path)


def ingest_anfr(con: duckdb.DuckDBPyConnection) -> None:
    session = http_session()
    url, title = latest_anfr_zip(session)
    log.info("archive ANFR la plus récente : %s", title)
    zpath = download(session, url, RAW_DIR / "anfr" / Path(url).name)
    if zpath is None:
        return
    out = RAW_DIR / "anfr" / zpath.stem
    if not out.exists():
        with zipfile.ZipFile(zpath) as z:
            z.extractall(out)
    files = [f for f in out.rglob("*") if f.suffix.lower() in (".csv", ".txt")]
    if not files:
        log.error("aucun CSV/TXT dans l'archive %s", zpath.name)
        return
    for f in files:
        load_csv(con, f, f"raw_anfr_{safe_name(f.stem)}")


# --------------------------------------------------------------------------- référentiel géographique

def ingest_geo(con: duckdb.DuckDBPyConnection) -> None:
    """Communes (avec arrondissements de Paris, Lyon, Marseille), départements et régions.
    Nécessaire pour dim_commune : les sources réseau ne donnent ni région ni population."""
    session = http_session()
    for table, path in GEO_ENDPOINTS.items():
        raw = download(session, GEO_API + path, RAW_DIR / "geo" / f"{table.removeprefix('raw_geo_')}.json")
        if raw is None:
            continue
        p = raw.as_posix()
        con.execute(f"""
            CREATE OR REPLACE TABLE {table} AS
            SELECT * FROM read_json('{p}', format = 'array', columns = {{
                {", ".join(f"'{c}': 'VARCHAR'" for c in _geo_columns(raw))}
            }})
        """)
        n = con.execute(f"SELECT count(*) FROM {table}").fetchall()[0][0]
        log.info("table %s : %d lignes", table, n)


def _geo_columns(path: Path) -> list[str]:
    """Colonnes présentes dans le JSON (toutes lues en texte, comme les autres tables brutes)."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    cols: dict[str, None] = {}
    for row in data:
        cols.update(dict.fromkeys(row))
    return list(cols)


# --------------------------------------------------------------------------- profil du schéma

def profile(con: duckdb.DuckDBPyConnection) -> None:
    """Écrit data/schema_profile.md : colonnes, types, volumétrie, exemples.
    Sert de base pour concevoir la couche staging et le schéma enrichi du LLM."""
    objs = con.execute("""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'main' ORDER BY table_name
    """).fetchall()
    lines = ["# Profil du schéma brut", "", f"Généré le {dt.datetime.now():%Y-%m-%d %H:%M}", ""]
    for (t,) in objs:
        n = con.execute(f'SELECT count(*) FROM "{t}"').fetchall()[0][0]
        lines += [f"## {t}", "", f"{n:,} lignes".replace(",", " "), "",
                  "| colonne | type | non nuls | distincts | exemple |", "|---|---|---|---|---|"]
        cols = con.execute(f'DESCRIBE "{t}"').fetchall()
        for c in cols:
            name, ctype = c[0], c[1]
            nn, nd, ex = con.execute(
                f'SELECT count("{name}"), approx_count_distinct("{name}"), any_value("{name}") FROM "{t}"'
            ).fetchall()[0]
            ex = str(ex)[:40].replace("|", "/") if ex is not None else ""
            lines.append(f"| {name} | {ctype} | {nn} | {nd} | {ex} |")
        lines.append("")
    PROFILE_PATH.write_text("\n".join(lines), encoding="utf-8")
    log.info("profil écrit dans %s", PROFILE_PATH)


# --------------------------------------------------------------------------- CLI

def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Ingestion des données réseau mobile dans DuckDB")
    p.add_argument("command", choices=["indispo", "anfr", "geo", "all", "profile"])
    p.add_argument("--days", type=int, default=30, help="nombre de jours à charger (jusqu'à hier)")
    p.add_argument("--start", type=dt.date.fromisoformat, help="date de début AAAA-MM-JJ")
    p.add_argument("--end", type=dt.date.fromisoformat, help="date de fin AAAA-MM-JJ")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    end = args.end or (dt.date.today() - dt.timedelta(days=1))
    start = args.start or (end - dt.timedelta(days=args.days - 1))

    con = duckdb.connect(str(DB_PATH))
    try:
        if args.command in ("indispo", "all"):
            ingest_indispo(con, start, end)
        if args.command in ("anfr", "all"):
            ingest_anfr(con)
        if args.command in ("geo", "all"):
            ingest_geo(con)
        if args.command in ("profile", "all"):
            profile(con)
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
