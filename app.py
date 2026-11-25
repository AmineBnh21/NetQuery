"""
Interface NetQuery (Streamlit), en mode conversation.

  streamlit run app.py
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from netquery.cache import QuestionCache
from netquery.llm import LLM
from netquery.pipeline import BACKEND, Answer, ask
from netquery.prompt import Turn

EXEMPLES = [
    "Combien de sites SFR étaient en panne 4G dans le Rhône la semaine dernière ?",
    "Quel opérateur a eu le plus d'indisponibilités en Auvergne-Rhône-Alpes depuis le 1er novembre ?",
    "Quels sites Orange sont en panne depuis plus de 30 jours ?",
    "Combien de sites ont reçu leur première antenne 5G par opérateur chaque trimestre de 2025 ?",
    "Nombre de sites en panne par jour la semaine dernière",
]
SUIVIS = ["Et pour Bouygues Telecom ?", "Et dans le Nord ?", "Par région ?", "Seulement la 5G"]

ERREURS = {"llm": "Modèle injoignable", "extraction": "Pas de requête dans la réponse",
           "securite": "Requête refusée par les garde-fous", "execution": "Erreur à l'exécution",
           "cout": "Requête trop coûteuse"}

st.set_page_config(page_title="NetQuery", page_icon="📡", layout="wide")


@st.cache_resource
def get_llm() -> LLM:
    return LLM()


@st.cache_resource
def get_cache() -> QuestionCache:
    return QuestionCache()


# --------------------------------------------------------------------------- affichage d'un résultat

def add_coordinates(df: pd.DataFrame) -> pd.DataFrame | None:
    """Coordonnées des sites d'un résultat (via dim_site), pour afficher une carte."""
    if {"lat", "lon"} <= set(df.columns):
        return df
    if "site_id" not in df.columns or df.empty:
        return None
    from netquery.sql_guard import readonly_connection

    ids = df["site_id"].dropna().astype(str).unique().tolist()[:1000]
    con = readonly_connection()
    try:
        coords = con.execute("select site_id, lat, lon from dim_site where site_id in ?", [ids]).df()
    finally:
        con.close()
    return df.merge(coords, on="site_id", how="left")


def show_map(points: pd.DataFrame) -> None:
    """Carte des sites, centrée sur leur étendue, avec une info-bulle par site."""
    import pydeck as pdk

    import math

    # cadrage sur 90 % des points (un site d'outre-mer ou isolé ne doit pas dézoomer toute la carte)
    lat_lo, lat_hi = points["lat"].quantile([0.05, 0.95])
    lon_lo, lon_hi = points["lon"].quantile([0.05, 0.95])
    span = max(lon_hi - lon_lo, (lat_hi - lat_lo) * 1.4, 0.05)
    zoom = min(12.0, max(3.0, math.log2(360 / span) - 0.8))
    tooltip_cols = [c for c in points.columns if c not in ("lat", "lon")][:4]
    layer = pdk.Layer(
        "ScatterplotLayer", data=points, get_position="[lon, lat]",
        get_fill_color=[231, 76, 60, 190], get_radius=3000, radius_min_pixels=3, radius_max_pixels=12,
        pickable=True,
    )
    view = pdk.ViewState(latitude=float((lat_lo + lat_hi) / 2), longitude=float((lon_lo + lon_hi) / 2), zoom=zoom)
    st.pydeck_chart(pdk.Deck(layers=[layer], initial_view_state=view,
                             tooltip={"text": "\n".join(f"{c} : {{{c}}}" for c in tooltip_cols)}),
                    height=380)
    st.caption(f"{len(points)} site(s) localisé(s)")


def show_chart(df: pd.DataFrame) -> None:
    """Carte, courbe ou barres quand la forme du résultat s'y prête."""
    if df.empty:
        return
    geo = add_coordinates(df)
    if geo is not None:
        points = geo.dropna(subset=["lat", "lon"])
        if not points.empty:
            show_map(points)
            return
    cols = list(df.columns)
    num = [c for c in cols if pd.api.types.is_numeric_dtype(df[c]) and not pd.api.types.is_bool_dtype(df[c])]
    dims = [c for c in cols if c not in num]
    if len(df) < 2 or not num or not dims:
        return
    x = dims[0]
    if len(dims) == 1:
        data = df.set_index(x)[num]
    elif len(dims) == 2 and len(num) == 1:
        data = df.pivot_table(index=x, columns=dims[1], values=num[0], aggfunc="sum")  # une série par valeur
    else:
        return
    if pd.api.types.is_datetime64_any_dtype(df[x]) or x in ("date", "trimestre", "mois", "annee"):
        st.line_chart(data)
    else:
        st.bar_chart(data)


def show_answer(ans: Answer) -> None:
    if ans.error_kind == "clarification":
        st.info(ans.error)
        return
    if not ans.ok:
        st.error(f"{ERREURS.get(ans.error_kind, 'Erreur')} : {ans.error}")
    else:
        df, n = ans.df, len(ans.df)
        if n == 1 and len(df.columns) == 1:
            v = df.iloc[0, 0]
            st.metric(df.columns[0], f"{v:,}".replace(",", " ") if isinstance(v, (int, float)) else v)
        else:
            show_chart(df)
            st.dataframe(df, use_container_width=True, hide_index=True)
            if ans.truncated:
                st.caption("Résultat tronqué au nombre maximal de lignes.")

    if ans.filters:
        st.markdown("**Compris :** " + " · ".join(ans.filters)
                    + ("  _(suite de la question précédente)_" if ans.followup else ""))
    if ans.warnings:
        st.warning("Filtre non demandé dans la question : " + " ; ".join(ans.warnings)
                   + ". Reformulez si ce n'est pas ce que vous vouliez.")
    if ans.sql:
        with st.expander("SQL"):
            st.code(ans.sql, language="sql")
            if ans.cost.get("sql_bigquery"):
                st.caption("Traduit pour BigQuery :")
                st.code(ans.cost["sql_bigquery"], language="sql")
    if ans.nb_corrections:
        with st.expander(f"Requête corrigée automatiquement ({ans.nb_corrections} fois)"):
            for i, a in enumerate(ans.attempts[:-1], 1):
                st.markdown(f"**Essai {i}** : {a.error}")
                if a.sql:
                    st.code(a.sql, language="sql")
    t = ans.timings
    st.caption(
        ("SQL repris du cache, sans appel au modèle · " if ans.from_cache else
         f"{len(ans.prompt.tables)} table(s) fournie(s) au modèle · "
         f"prompt ≈ {ans.prompt.size_chars // 4} tokens · LLM {t.get('llm_s', 0):.1f} s · ")
        + f"exécution {t.get('exec_s', 0) * 1000:.0f} ms"
        + (f" · BigQuery : {ans.cost['bytes_processed'] / 1e3:.0f} Ko lus"
           + (" (cache BigQuery)" if ans.cost.get("cache_hit") else "")
           if "bytes_processed" in ans.cost else "")
    )


# --------------------------------------------------------------------------- page

llm = get_llm()
history: list[tuple[str, Answer]] = st.session_state.setdefault("history", [])

with st.sidebar:
    st.title("📡 NetQuery")
    st.caption("Questions en français sur les pannes et le déploiement des réseaux mobiles "
               "(Arcep, ANFR).")
    st.subheader("Exemples")
    for ex in EXEMPLES:
        if st.button(ex, use_container_width=True):
            st.session_state["pending"] = ex
    st.subheader("Questions de suivi")
    for ex in SUIVIS:
        if st.button(ex, use_container_width=True, disabled=not history):
            st.session_state["pending"] = ex
    st.divider()
    if st.button("Nouvelle conversation", use_container_width=True):
        history.clear()
    st.caption(f"Modèle : `{llm.model}`  \nExécution : `{BACKEND}`")

if not history:
    st.markdown("### Posez une question sur les réseaux mobiles français")
    st.caption("Exemple : « Combien de sites Orange étaient en panne hier dans le Nord ? », "
               "puis « Et pour SFR ? »")

for q, a in history:
    with st.chat_message("user"):
        st.markdown(q)
    with st.chat_message("assistant"):
        show_answer(a)

question = st.chat_input("Votre question") or st.session_state.pop("pending", None)
if question:
    with st.chat_message("user"):
        st.markdown(question)
    previous = next((Turn(q, a.sql) for q, a in reversed(history) if a.ok and a.sql), None)
    with st.chat_message("assistant"):
        with st.spinner("Génération et exécution de la requête..."):
            ans = ask(question, llm=llm, cache=get_cache(), previous=previous)
        show_answer(ans)
    history.append((question, ans))
