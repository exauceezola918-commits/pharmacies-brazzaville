import pandas as pd
import streamlit as st
import folium
from streamlit_folium import st_folium
from datetime import date
from math import radians, sin, cos, sqrt, atan2

st.set_page_config(page_title="Pharmacies Brazzaville", page_icon="💊", layout="wide")

DATA_PATH = "pharmacies.csv"


@st.cache_data
def load_data(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["garde_debut"] = pd.to_datetime(df["garde_debut"], errors="coerce")
    df["garde_fin"] = pd.to_datetime(df["garde_fin"], errors="coerce")
    return df


def est_de_garde_aujourdhui(row, aujourdhui) -> bool:
    """Une pharmacie 'garde' n'est affichée comme active que si la date du jour
    tombe dans sa période de garde. Une pharmacie 'jour' est toujours active
    (elle est simplement ouverte aux horaires normaux)."""
    if row["statut"] == "jour":
        return True
    if pd.isna(row["garde_debut"]) or pd.isna(row["garde_fin"]):
        return False
    return row["garde_debut"].date() <= aujourdhui <= row["garde_fin"].date()


def distance_km(lat1, lon1, lat2, lon2) -> float:
    R = 6371
    dlat, dlon = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


df = load_data(DATA_PATH)
aujourdhui = date.today()
df["active_aujourdhui"] = df.apply(lambda r: est_de_garde_aujourdhui(r, aujourdhui), axis=1)

st.title("💊 Pharmacies de Brazzaville")
st.caption(
    "Prototype avec des données fictives — à remplacer par la vraie base "
    "(pharmacies.csv) une fois constituée. La structure du fichier ne doit pas changer."
)

# --- Filtres ---
col1, col2, col3 = st.columns([1, 1, 2])

with col1:
    filtre_statut = st.radio(
        "Type",
        options=["Toutes", "De jour", "De garde / nuit"],
        horizontal=False,
    )

with col2:
    quartiers = ["Tous"] + sorted(df["quartier"].unique().tolist())
    filtre_quartier = st.selectbox("Quartier", quartiers)

with col3:
    recherche = st.text_input("Rechercher une pharmacie par nom")

only_active = st.checkbox("Afficher uniquement les pharmacies actives aujourd'hui", value=True)

# --- Application des filtres ---
resultat = df.copy()

if filtre_statut == "De jour":
    resultat = resultat[resultat["statut"] == "jour"]
elif filtre_statut == "De garde / nuit":
    resultat = resultat[resultat["statut"] == "garde"]

if filtre_quartier != "Tous":
    resultat = resultat[resultat["quartier"] == filtre_quartier]

if recherche:
    resultat = resultat[resultat["nom"].str.contains(recherche, case=False, na=False)]

if only_active:
    resultat = resultat[resultat["active_aujourdhui"]]

st.markdown(f"**{len(resultat)} pharmacie(s) trouvée(s)**")

# --- Carte ---
if len(resultat) > 0:
    centre_lat = resultat["latitude"].mean()
    centre_lon = resultat["longitude"].mean()
else:
    centre_lat, centre_lon = -4.2634, 15.2429  # Brazzaville centre

carte = folium.Map(location=[centre_lat, centre_lon], zoom_start=12, tiles="cartodbpositron")

for _, row in resultat.iterrows():
    couleur = "green" if row["statut"] == "jour" else "orange"
    popup = folium.Popup(
        f"<b>{row['nom']}</b><br>{row['adresse']}<br>{row['quartier']}<br>"
        f"📞 {row['telephone']}<br>Statut : {row['statut'].capitalize()}",
        max_width=250,
    )
    folium.Marker(
        location=[row["latitude"], row["longitude"]],
        popup=popup,
        tooltip=row["nom"],
        icon=folium.Icon(color=couleur, icon="plus-sign"),
    ).add_to(carte)

st_folium(carte, width=None, height=450)

# --- Liste détaillée ---
st.subheader("Liste des pharmacies")

for _, row in resultat.sort_values("nom").iterrows():
    badge = "🟢 Ouverte (jour)" if row["statut"] == "jour" else "🟠 De garde"
    if row["statut"] == "garde" and not row["active_aujourdhui"]:
        badge = "⚪ Garde hors période"
    with st.container(border=True):
        c1, c2 = st.columns([3, 1])
        with c1:
            st.markdown(f"**{row['nom']}** — {row['quartier']}")
            st.caption(f"{row['adresse']} · 📞 {row['telephone']}")
        with c2:
            st.markdown(badge)

if len(resultat) == 0:
    st.info("Aucune pharmacie ne correspond aux critères sélectionnés.")

st.divider()
st.caption(
    "Pour mettre à jour les pharmacies de garde : éditez pharmacies.csv "
    "(colonnes garde_debut / garde_fin au format AAAA-MM-JJ)."
)
