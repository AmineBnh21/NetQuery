"""
Interface NetQuery (Streamlit).

  streamlit run app.py
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from netquery.llm import LLM
from netquery.pipeline import ask

EXEMPLES = [
    "Combien de sites SFR étaient en panne 4G dans le Rhône la semaine dernière ?",
    "Quel opérateur a eu le plus d'indisponibilités en Auvergne-Rhône-Alpes depuis le 1er novembre ?",
    "Combien de sites ont reçu leur première antenne 5G par opérateur chaque trimestre de 2025 ?",
    "Quelles communes ont des sites hors service depuis plus de 7 jours ?",
    "Nombre de sites en panne par jour la semaine dernière",
]

st.set_page_config(page_title="NetQuery", page_icon="📡", layout="wide")
st.title("NetQuery")
st.caption("Interroger les données des réseaux mobiles français en langage naturel "
           "(Arcep sites indisponibles, ANFR stations).")


@st.cache_resource
def get_llm() -> LLM:
    return LLM()


def show_chart(df: pd.DataFrame) -> None:
    """Graphique simple quand la forme du résultat s'y prête."""
    if df.empty:
        return
    cols = list(df.columns)
    if {"lat", "lon"} <= set(cols):
        st.map(df.rename(columns={"lon": "longitude", "lat": "latitude"}).dropna(subset=["latitude", "longitude"]))
        return
    num = [c for c in cols if pd.api.types.is_numeric_dtype(df[c]) and not pd.api.types.is_bool_dtype(df[c])]
    dims = [c for c in cols if c not in num]
    if len(df) < 2 or not num or not dims:
        return
    x = dims[0]
    if len(dims) == 1:
        data = df.set_index(x)[num]
    elif len(dims) == 2 and len(num) == 1:
        # ex. date x opérateur -> une série par opérateur
        data = df.pivot_table(index=x, columns=dims[1], values=num[0], aggfunc="sum")
    else:
        return
    if pd.api.types.is_datetime64_any_dtype(df[x]) or x in ("date", "trimestre", "mois", "annee"):
        st.line_chart(data)
    else:
        st.bar_chart(data)


with st.sidebar:
    st.subheader("Exemples")
    for ex in EXEMPLES:
        if st.button(ex, use_container_width=True):
            st.session_state["question"] = ex
    llm = get_llm()
    st.divider()
    st.caption(f"Modèle : `{llm.model}`  \nServeur : `{llm.base_url}`")

question = st.text_input("Votre question", key="question",
                         placeholder="Ex. combien de sites Orange en panne hier dans le Nord ?")

if question:
    with st.spinner("Génération et exécution de la requête..."):
        ans = ask(question, llm=llm)

    if ans.ok:
        n = len(ans.df)
        st.success(f"{n} ligne{'s' if n > 1 else ''}" + (" (résultat tronqué)" if ans.truncated else ""))
        if n == 1 and len(ans.df.columns) == 1:
            st.metric(ans.df.columns[0], f"{ans.df.iloc[0, 0]:,}".replace(",", " ")
                      if isinstance(ans.df.iloc[0, 0], (int, float)) else ans.df.iloc[0, 0])
        else:
            show_chart(ans.df)
            st.dataframe(ans.df, use_container_width=True, hide_index=True)
    else:
        labels = {"llm": "Modèle injoignable", "extraction": "Pas de requête dans la réponse",
                  "securite": "Requête refusée par les garde-fous", "execution": "Erreur à l'exécution"}
        st.error(f"{labels.get(ans.error_kind, 'Erreur')} : {ans.error}")

    if ans.sql:
        st.code(ans.sql, language="sql")
    if ans.nb_corrections:
        with st.expander(f"Requête corrigée automatiquement ({ans.nb_corrections} fois)"):
            for i, a in enumerate(ans.attempts[:-1], 1):
                st.markdown(f"**Essai {i}** : {a.error}")
                if a.sql:
                    st.code(a.sql, language="sql")
    t = ans.timings
    st.caption(
        f"Tables fournies au modèle : {', '.join(ans.prompt.tables)} · "
        f"prompt ≈ {ans.prompt.size_chars // 4} tokens · "
        f"LLM {t.get('llm_s', 0):.1f} s · exécution {t.get('exec_s', 0) * 1000:.0f} ms"
    )
    with st.expander("Détails (prompt et réponse brute)"):
        for m in ans.prompt.messages:
            st.markdown(f"**{m['role']}**")
            st.text(m["content"])
        st.markdown("**réponse du modèle**")
        st.text(ans.raw_output or "(aucune)")
