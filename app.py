import hmac
import html
import io
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from xml.sax.saxutils import escape as xml_escape
from zoneinfo import ZoneInfo

import folium
import numpy as np
import pandas as pd
import streamlit as st
from folium.plugins import FastMarkerCluster
from PIL import Image, ImageDraw, ImageFont
from streamlit_folium import st_folium
from streamlit_geolocation import streamlit_geolocation

# Imports optionnels pour la génération PDF
try:
    from reportlab.lib.pagesizes import letter, landscape, A4
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib import colors
    REPORTLAB_AVAILABLE = True
except ImportError:
    REPORTLAB_AVAILABLE = False

# ==========================================
# CONSTANTES
# ==========================================
CSV_PATH = "pharmacies.csv"                   # source initiale (seed) des pharmacies
SIGNALEMENTS_CSV_LEGACY = "signalements.csv"  # ancien stockage (importé une seule fois)
DB_PATH = os.environ.get("PHARMACIES_DB_PATH", "pharmacies.db")
LOGO_PATH = "logo.png"                        # optionnel : dépose ton logo ici

FUSEAU = "Africa/Brazzaville"                 # UTC+1, sans heure d'été
SEUIL_CLUSTER = 150                           # au-delà : carte en mode « regroupement »
LIMITE_LISTE = 30                             # nombre de cartes affichées dans la liste

COLONNES_REQUISES = [
    "nom", "quartier", "adresse", "telephone", "statut",
    "latitude", "longitude", "garde_debut", "garde_fin",
]
# Horaires par défaut si le CSV n'a pas de colonnes heure_ouverture / heure_fermeture
HORAIRES_JOUR = (8 * 60, 20 * 60)   # 08:00 - 20:00
HORAIRES_GARDE = (0, 24 * 60)       # 24h/24 pendant la période de garde

JOURS_FR = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]

MEDICAMENTS_DEFAULT = [
    "Paracétamol, Insuline, Amoxicilline, Ibuprofène",
    "Paracétamol, Artéméther/Luméfantrine, Ventoline",
    "Insuline, Sérum Physiologique, Oméprazole",
    "Paracétamol, Ibuprofène, Amoxicilline, Ventoline",
    "Artéméther/Luméfantrine, Sérum Physiologique, Oméprazole",
]

CALLBACK_CLUSTER = """
function (row) {
    var marker = L.circleMarker(new L.LatLng(row[0], row[1]),
        {radius: 9, color: row[3], fillColor: row[3], fillOpacity: 0.85, weight: 2});
    marker.bindPopup(row[2], {maxWidth: 290});
    marker.bindTooltip(row[4]);
    return marker;
}
"""


def _icone_page():
    """Utilise logo.png comme icône d'onglet s'il existe, sinon un emoji."""
    if os.path.exists(LOGO_PATH):
        try:
            return Image.open(LOGO_PATH)
        except Exception:
            pass
    return "💊"


# --- Configuration de la page (doit être le premier appel Streamlit) ---
st.set_page_config(page_title="Pharmacies Brazzaville", page_icon=_icone_page(), layout="wide")


def afficher_logo_sidebar():
    """Logo de l'app (logo.png) dans la barre latérale ; ancienne icône en secours si le fichier manque."""
    if os.path.exists(LOGO_PATH):
        st.image(LOGO_PATH, width=70)
    else:
        st.image("https://img.icons8.com/color/96/pharmacy-shop.png", width=70)


def appliquer_theme_sombre():
    """Applique un thème sombre fixe sur l'ensemble de l'application."""
    css = """
    <style>
    /* Fond principal sombre */
    .stAppViewContainer, .main {
        background-color: #121212 !important;
        color: #e0e0e0 !important;
    }
    
    /* Barre latérale sombre */
    [data-testid="stSidebar"] {
        background-color: #1e1e1e !important;
        border-right: 1px solid #333333;
    }
    [data-testid="stSidebar"] * {
        color: #f1f1f1 !important;
    }
    [data-testid="stSidebar"] input {
        background-color: #2b2b2b !important;
        color: #ffffff !important;
        border: 1px solid #444444 !important;
    }

    /* Titres et textes */
    h1, h2, h3, h4, h5, h6, label, .stMarkdown p {
        color: #81c784 !important;
    }

    /* Cartes/Conteneurs des pharmacies */
    [data-testid="stVerticalBlockBorderWrapper"] {
        border-left: 5px solid #4caf50 !important;
        background-color: #1e1e1e !important;
        box-shadow: 0 2px 4px rgba(0,0,0,0.5);
        color: #ffffff !important;
    }

    /* Badges de médicaments */
    .med-badge {
        display: inline-block;
        background-color: #2e7d32;
        color: #ffffff;
        padding: 3px 8px;
        margin: 2px;
        border-radius: 12px;
        font-size: 11px;
        font-weight: 600;
    }

    /* Boutons HTML (Appeler & Itinéraire) */
    .btn-action {
        display: inline-block;
        padding: 6px 12px;
        margin-right: 5px; 
        margin-top: 5px;
        font-size: 13px; 
        font-weight: bold; 
        text-align: center;
        text-decoration: none !important;
        border-radius: 6px;
    }
    .btn-call { background-color: #2e7d32; color: white !important; }
    .btn-call:hover { background-color: #1b5e20; }
    .btn-maps { background-color: #0288d1; color: white !important; }
    .btn-maps:hover { background-color: #01579b; }

    /* Footer fixe */
    .footer {
        position: fixed;
        left: 0; bottom: 0; width: 100%;
        background-color: #1e1e1e;
        color: #81c784;
        text-align: center;
        padding: 8px 0px; 
        font-size: 13px;
        z-index: 999;
        border-top: 1px solid #333;
    }
    
    .main .block-container { padding-bottom: 70px; }
    </style>
    """
    st.markdown(css, unsafe_allow_html=True)


# ==========================================
# HEURE LOCALE (Brazzaville)
# ==========================================
def maintenant_local() -> datetime:
    """Heure de Brazzaville (naïve). Le serveur Streamlit Cloud est en UTC : on ne se fie pas à son horloge locale."""
    try:
        return datetime.now(ZoneInfo(FUSEAU)).replace(tzinfo=None, microsecond=0)
    except Exception:
        return (datetime.now(timezone.utc) + timedelta(hours=1)).replace(tzinfo=None, microsecond=0)


def il_y_a(passe: datetime, maintenant: datetime) -> str:
    s = max(int((maintenant - passe).total_seconds()), 0)
    if s < 60:
        return "à l'instant"
    if s < 3600:
        return f"il y a {s // 60} min"
    if s < 86400:
        return f"il y a {s // 3600} h"
    j = s // 86400
    if j < 14:
        return f"il y a {j} jour{'s' if j > 1 else ''}"
    if j < 60:
        return f"il y a {j // 7} semaines"
    return f"il y a {j // 30} mois"


def fmt_minutes(m: int) -> str:
    return f"{int(m) // 60:02d}:{int(m) % 60:02d}"


# ==========================================
# BASE DE DONNÉES SQLITE
# ==========================================
@contextmanager
def get_conn():
    """Connexion SQLite avec commit/rollback automatique et fermeture garantie."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _table_existe(conn, nom_table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (nom_table,)
    ).fetchone()
    return row is not None


def _colonnes_table(conn, table: str) -> list:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def lire_csv_securise(path: str) -> pd.DataFrame:
    """Lit pharmacies.csv sans planter : fichier absent, vide, corrompu, mauvais encodage,
    colonnes manquantes ou coordonnées invalides sont gérés avec un message clair."""
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Le fichier « {path} » est introuvable. Vérifie qu'il est bien à la racine du projet, "
            "dans le même dossier que app.py."
        )

    df = None
    for encodage in ("utf-8-sig", "latin-1"):
        try:
            df = pd.read_csv(path, encoding=encodage, on_bad_lines="skip", dtype={"telephone": str})
            break
        except UnicodeDecodeError:
            continue
        except pd.errors.EmptyDataError:
            raise ValueError(f"Le fichier « {path} » est vide.")
        except pd.errors.ParserError as e:
            raise ValueError(f"Le fichier « {path} » est corrompu (lecture impossible : {e}).")
    if df is None:
        raise ValueError(f"Le fichier « {path} » a un encodage illisible (utilise UTF-8).")

    df.columns = [str(c).strip().lower() for c in df.columns]
    manquantes = [c for c in COLONNES_REQUISES if c not in df.columns]
    if manquantes:
        raise ValueError(f"Colonnes manquantes dans « {path} » : {', '.join(manquantes)}.")

    for col in ("latitude", "longitude"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    avant = len(df)
    df = df.dropna(subset=["nom", "latitude", "longitude"])
    df = df[df["latitude"].between(-90, 90) & df["longitude"].between(-180, 180)]
    if avant - len(df):
        logging.getLogger(__name__).warning("%d ligne(s) ignorée(s) (coordonnées invalides).", avant - len(df))
    if df.empty:
        raise ValueError(f"Aucune ligne exploitable dans « {path} ».")

    df["statut"] = df["statut"].astype(str).str.strip().str.lower()
    df["telephone"] = df["telephone"].fillna("").astype(str).str.replace(r"\.0$", "", regex=True)
    return df.reset_index(drop=True)


def _importer_pharmacies_csv(conn):
    """Charge pharmacies.csv dans la table SQL 'pharmacies'."""
    df = lire_csv_securise(CSV_PATH)

    # Dates stockées en texte ISO (AAAA-MM-JJ)
    for col in ("garde_debut", "garde_fin"):
        df[col] = pd.to_datetime(df[col], errors="coerce").dt.strftime("%Y-%m-%d")
        df[col] = df[col].where(df[col].notna(), None)

    if "stock_medicaments" not in df.columns:
        df["stock_medicaments"] = [
            MEDICAMENTS_DEFAULT[i % len(MEDICAMENTS_DEFAULT)] for i in range(len(df))
        ]
    else:
        df["stock_medicaments"] = df["stock_medicaments"].fillna("")

    df.to_sql("pharmacies", conn, if_exists="replace", index=False)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pharmacies_quartier ON pharmacies(quartier)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pharmacies_statut ON pharmacies(statut)")


@st.cache_resource(show_spinner=False)
def _initialiser_base():
    """Crée les tables et importe les CSV. En cas d'échec, l'exception n'est PAS mise en cache :
    l'app réessaiera au prochain rechargement (utile quand tu corriges le CSV)."""
    # 1) Signalements (indépendants des pharmacies)
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS signalements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date_signalement TEXT NOT NULL,
                pharmacie TEXT NOT NULL,
                type_erreur TEXT NOT NULL,
                description TEXT NOT NULL,
                contact_citoyen TEXT,
                statut_validation TEXT NOT NULL DEFAULT 'en_attente',
                date_validation TEXT
            )
            """
        )
        # Mise à niveau d'une base créée par la version précédente
        colonnes = _colonnes_table(conn, "signalements")
        if "statut_validation" not in colonnes:
            conn.execute("ALTER TABLE signalements ADD COLUMN statut_validation TEXT NOT NULL DEFAULT 'en_attente'")
        if "date_validation" not in colonnes:
            conn.execute("ALTER TABLE signalements ADD COLUMN date_validation TEXT")

        nb = conn.execute("SELECT COUNT(*) FROM signalements").fetchone()[0]
        if nb == 0 and os.path.exists(SIGNALEMENTS_CSV_LEGACY):
            try:
                ancien = pd.read_csv(SIGNALEMENTS_CSV_LEGACY)
                cols = ["date_signalement", "pharmacie", "type_erreur", "description", "contact_citoyen"]
                if set(cols).issubset(ancien.columns):
                    ancien[cols].to_sql("signalements", conn, if_exists="append", index=False)
            except Exception:
                pass

    # 2) Pharmacies (import depuis le CSV seulement si la table est absente ou vide)
    with get_conn() as conn:
        if not _table_existe(conn, "pharmacies") or conn.execute("SELECT COUNT(*) FROM pharmacies").fetchone()[0] == 0:
            _importer_pharmacies_csv(conn)
    return True


def preparer_base():
    """Retourne None si tout va bien, sinon un message d'erreur lisible."""
    try:
        _initialiser_base()
        return None
    except (FileNotFoundError, ValueError) as e:
        return str(e)
    except Exception as e:
        logging.getLogger(__name__).exception("Initialisation de la base impossible")
        return f"{type(e).__name__} : {e}"


@st.cache_data(ttl=600, show_spinner=False)
def load_data() -> pd.DataFrame:
    """Lit la table SQL 'pharmacies' (cache 10 min) et prépare les colonnes."""
    with get_conn() as conn:
        df = pd.read_sql_query("SELECT * FROM pharmacies", conn)

    manquantes = [c for c in COLONNES_REQUISES if c not in df.columns]
    if manquantes:
        raise ValueError(f"La table 'pharmacies' est incomplète (colonnes manquantes : {', '.join(manquantes)}).")

    df["garde_debut"] = pd.to_datetime(df["garde_debut"], errors="coerce")
    df["garde_fin"] = pd.to_datetime(df["garde_fin"], errors="coerce")
    df["latitude"] = pd.to_numeric(df["latitude"], errors="coerce")
    df["longitude"] = pd.to_numeric(df["longitude"], errors="coerce")
    df = df.dropna(subset=["latitude", "longitude"]).reset_index(drop=True)
    df["statut"] = df["statut"].astype(str).str.strip().str.lower()
    if "stock_medicaments" not in df.columns:
        df["stock_medicaments"] = ""
    df["stock_medicaments"] = df["stock_medicaments"].fillna("")

    # Horaires en minutes depuis minuit (colonnes optionnelles heure_ouverture / heure_fermeture)
    est_garde = (df["statut"] == "garde").to_numpy()

    def _minutes(col, def_jour, def_garde):
        defaut = pd.Series(np.where(est_garde, def_garde, def_jour), index=df.index)
        if col not in df.columns:
            return defaut.astype(int)
        t = pd.to_datetime(df[col].astype(str).str.slice(0, 5), format="%H:%M", errors="coerce")
        return (t.dt.hour * 60 + t.dt.minute).fillna(defaut).astype(int)

    df["min_ouverture"] = _minutes("heure_ouverture", HORAIRES_JOUR[0], HORAIRES_GARDE[0])
    df["min_fermeture"] = _minutes("heure_fermeture", HORAIRES_JOUR[1], HORAIRES_GARDE[1])
    return df


def charger_donnees_securise():
    """Retourne (DataFrame, message_erreur). Ne lève jamais d'exception."""
    vide = pd.DataFrame(columns=COLONNES_REQUISES)
    erreur = preparer_base()
    if erreur:
        return vide, erreur
    try:
        return load_data(), None
    except Exception as e:
        logging.getLogger(__name__).exception("Lecture des pharmacies impossible")
        return vide, f"Lecture de la base impossible : {e}"


# --- Signalements ---
def enregistrer_signalement(nom_pharmacie, type_erreur, description, contact):
    """Enregistre une requête de correction (en attente de validation)."""
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO signalements
                (date_signalement, pharmacie, type_erreur, description, contact_citoyen)
            VALUES (?, ?, ?, ?, ?)
            """,
            (maintenant_local().strftime("%Y-%m-%d %H:%M:%S"), nom_pharmacie, type_erreur, description, contact),
        )


def changer_statut_signalement(id_sig: int, statut: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE signalements SET statut_validation = ?, date_validation = ? WHERE id = ?",
            (statut, maintenant_local().strftime("%Y-%m-%d %H:%M:%S"), int(id_sig)),
        )
    charger_verifications.clear()


def marquer_pharmacie_verifiee(nom_pharmacie: str):
    """Vérification de routine par un administrateur (sans signalement citoyen)."""
    maintenant = maintenant_local().strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO signalements
                (date_signalement, pharmacie, type_erreur, description, contact_citoyen,
                 statut_validation, date_validation)
            VALUES (?, ?, 'Vérification administrateur', 'Vérification de routine', '', 'valide', ?)
            """,
            (maintenant, nom_pharmacie, maintenant),
        )
    charger_verifications.clear()


def lire_signalements(en_attente: bool) -> pd.DataFrame:
    requete = (
        "SELECT * FROM signalements WHERE statut_validation = 'en_attente' ORDER BY id DESC"
        if en_attente
        else "SELECT * FROM signalements WHERE statut_validation != 'en_attente' ORDER BY id DESC LIMIT 20"
    )
    with get_conn() as conn:
        return pd.read_sql_query(requete, conn)


@st.cache_data(ttl=60, show_spinner=False)
def charger_verifications() -> dict:
    """{nom_pharmacie: date du dernier signalement validé}."""
    try:
        with get_conn() as conn:
            lignes = conn.execute(
                """
                SELECT pharmacie, MAX(date_validation)
                FROM signalements
                WHERE statut_validation = 'valide' AND date_validation IS NOT NULL
                GROUP BY pharmacie
                """
            ).fetchall()
    except Exception:
        return {}
    resultat = {}
    for nom, d in lignes:
        try:
            resultat[nom] = datetime.strptime(d, "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            pass
    return resultat


def badge_verification(derniere, maintenant: datetime):
    """Retourne (texte, couleur) du badge de fiabilité."""
    if derniere is None:
        return "⚪ Pas encore vérifié", "#9e9e9e"
    texte = il_y_a(derniere, maintenant)
    if (maintenant - derniere).days <= 30:
        return f"✅ Informations vérifiées {texte}", "#66bb6a"
    return f"⚠️ Dernière vérification {texte}", "#ffa726"


# ==========================================
# CALCULS VECTORISÉS (numpy / pandas)
# ==========================================
def haversine_np(lat1, lon1, lat2, lon2):
    """Distance Haversine en km, vectorisée : lat2/lon2 peuvent être des tableaux numpy."""
    R = 6371.0
    lat1, lon1, lat2, lon2 = map(np.radians, (lat1, lon1, lat2, lon2))
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = np.sin(dlat / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2) ** 2
    a = np.clip(a, 0.0, 1.0)
    return R * 2 * np.arctan2(np.sqrt(a), np.sqrt(1 - a))


def masque_actif(df: pd.DataFrame, d_cible: date) -> pd.Series:
    """Pharmacie de jour, ou de garde à la date cible."""
    d = pd.Timestamp(d_cible)
    en_periode = (
        df["garde_debut"].notna()
        & df["garde_fin"].notna()
        & (df["garde_debut"].dt.normalize() <= d)
        & (d <= df["garde_fin"].dt.normalize())
    )
    return (df["statut"] == "jour") | en_periode


def masque_ouvert_maintenant(df: pd.DataFrame, maintenant: datetime) -> pd.Series:
    """Ouverte à la minute près : active aujourd'hui ET heure courante dans la plage d'ouverture
    (gère les plages qui passent minuit, ex. 20:00 -> 06:00)."""
    minute = maintenant.hour * 60 + maintenant.minute
    o, f = df["min_ouverture"], df["min_fermeture"]
    plage_normale = (o <= f) & (o <= minute) & (minute < f)
    plage_nuit = (o > f) & ((minute >= o) | (minute < f))
    return masque_actif(df, maintenant.date()) & (plage_normale | plage_nuit)


@st.cache_data(show_spinner=False)
def rechercher_pharmacies(
    df: pd.DataFrame,
    filtre_statut: str,
    filtre_quartier: str,
    recherche_nom: str,
    recherche_medoc: str,
    only_active: bool,
    ouvertes_maintenant: bool,
    user_lat,
    user_lon,
    maintenant: datetime,
) -> pd.DataFrame:
    """Filtre + distances + tri, mis en cache. `maintenant` (arrondi à la minute) fait partie
    de la clé de cache : le résultat est donc recalculé au plus une fois par minute."""
    res = df.copy()
    res["active_aujourdhui"] = masque_actif(res, maintenant.date())
    res["ouvert_maintenant"] = masque_ouvert_maintenant(res, maintenant)

    if filtre_statut == "De jour":
        res = res[res["statut"] == "jour"]
    elif filtre_statut == "De garde / nuit":
        res = res[res["statut"] == "garde"]

    if filtre_quartier != "Tous":
        res = res[res["quartier"] == filtre_quartier]

    if recherche_nom:
        res = res[res["nom"].str.contains(recherche_nom, case=False, na=False, regex=False)]

    if recherche_medoc:
        res = res[res["stock_medicaments"].str.contains(recherche_medoc, case=False, na=False, regex=False)]

    if only_active:
        res = res[res["active_aujourdhui"]]

    if ouvertes_maintenant:
        res = res[res["ouvert_maintenant"]]

    res = res.copy()
    if user_lat is not None and len(res) > 0:
        res["distance_km"] = haversine_np(
            user_lat, user_lon, res["latitude"].to_numpy(), res["longitude"].to_numpy()
        )
        res = res.sort_values("distance_km")
    else:
        res["distance_km"] = np.nan
        res = res.sort_values("nom")

    return res.reset_index(drop=True)


# ==========================================
# AFFICHAGE : états, popups, carte
# ==========================================
def etat_ouverture(row: dict) -> str:
    if row["ouvert_maintenant"]:
        return "🟢 Ouverte maintenant"
    if row["statut"] == "garde" and not row["active_aujourdhui"]:
        return "⚪ Garde hors période"
    return "🔴 Fermée actuellement"


def horaires_label(row: dict) -> str:
    if row["min_ouverture"] == 0 and row["min_fermeture"] == 24 * 60:
        return "24h/24"
    return f"{fmt_minutes(row['min_ouverture'])} – {fmt_minutes(row['min_fermeture'])}"


def couleur_statut(statut: str) -> str:
    return "#2e7d32" if statut == "jour" else "#ef6c00"


def html_popup(row: dict) -> str:
    """Popup de la carte. Styles en ligne car la carte s'affiche dans un iframe (sans le CSS de la page)."""
    gmaps_url = f"https://www.google.com/maps/dir/?api=1&destination={row['latitude']},{row['longitude']}"
    tel_clean = str(row["telephone"]).replace(" ", "").replace("-", "")
    distance = (
        f"<span style='font-size:12px;'>Distance : <b>{row['distance_km']:.1f} km</b></span><br>"
        if pd.notna(row["distance_km"]) else ""
    )
    style_btn = "display:inline-block;padding:6px 12px;margin-right:5px;color:#fff;border-radius:6px;text-decoration:none;font-size:13px;font-weight:bold;"
    return f"""
    <div style="font-family: Arial, sans-serif; min-width: 200px;">
        <b style="font-size: 14px;">{html.escape(str(row['nom']))}</b><br>
        <span style="font-size: 12px; color: #555;">{html.escape(str(row['adresse']))} ({html.escape(str(row['quartier']))})</span><br>
        <span style="font-size: 12px;">{etat_ouverture(row)} · {horaires_label(row)}</span><br>
        <div style="margin-top: 5px; font-size: 11px; color: #2e7d32;">
            <b>💊 Stocks :</b> <i>{html.escape(str(row['stock_medicaments']))}</i>
        </div>
        {distance}
        <div style="margin-top: 8px;">
            <a href="tel:{tel_clean}" target="_blank" style="{style_btn}background:#2e7d32;">📞 Appeler</a>
            <a href="{gmaps_url}" target="_blank" style="{style_btn}background:#0288d1;">🗺️ Itinéraire</a>
        </div>
    </div>
    """


def construire_carte(lignes: list, user_lat, user_lon, centre_lat, centre_lon) -> folium.Map:
    carte = folium.Map(location=[centre_lat, centre_lon], zoom_start=13 if user_lat else 12, tiles="OpenStreetMap")

    if user_lat is not None:
        folium.Marker(location=[user_lat, user_lon], tooltip="Vous êtes ici", icon=folium.Icon(color="blue", icon="user", prefix="fa")).add_to(carte)

    if len(lignes) > SEUIL_CLUSTER:
        # Beaucoup de pharmacies : un seul bloc JavaScript, marqueurs regroupés (rendu rapide)
        donnees = [
            [r["latitude"], r["longitude"], html_popup(r), couleur_statut(r["statut"]), str(r["nom"])]
            for r in lignes
        ]
        FastMarkerCluster(donnees, callback=CALLBACK_CLUSTER).add_to(carte)
    else:
        for r in lignes:
            couleur = "green" if r["statut"] == "jour" else "orange"
            folium.Marker(
                location=[r["latitude"], r["longitude"]],
                popup=folium.Popup(html_popup(r), max_width=290),
                tooltip=str(r["nom"]),
                icon=folium.Icon(color=couleur, icon="plus-sign"),
            ).add_to(carte)
    return carte


@st.dialog("🚨 Signaler une erreur")
def dialog_signalement(nom_pharmacie):
    st.write(f"Signaler une inexactitude pour : **{nom_pharmacie}**")
    type_err = st.selectbox("Type de problème", ["Erreur d'adresse/géolocalisation", "Horaires / Garde incorrects", "Numéro de téléphone erroné", "Stock de médicaments non à jour", "Autre"])
    desc = st.text_area("Description / Correction proposée", placeholder="Saisissez les informations exactes...")
    contact = st.text_input("Votre contact (Optionnel)", placeholder="Téléphone ou E-mail")
    
    if st.button("Envoyer le signalement", type="primary"):
        if desc.strip():
            enregistrer_signalement(nom_pharmacie, type_err, desc.strip(), contact.strip())
            st.toast("Merci ! Votre signalement a été transmis aux équipes de mise à jour.", icon="✅")
            st.rerun()
        else:
            st.warning("Veuillez fournir une courte description du problème.")


# ==========================================
# EXPORTS : PDF officiel + planning hors-ligne (PDF / image)
# ==========================================
@st.cache_data(show_spinner=False)
def generer_pdf(df_export: pd.DataFrame) -> bytes:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=landscape(letter), rightMargin=30, leftMargin=30, topMargin=30, bottomMargin=30)
    elements = []
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle('TitleStyle', parent=styles['Heading1'], fontName='Helvetica-Bold', fontSize=16, textColor=colors.HexColor('#1b5e20'), spaceAfter=10)

    elements.append(Paragraph("<b>Tableau Officiel des Tours de Garde — Pharmacies de Brazzaville</b>", title_style))
    elements.append(Paragraph(f"Généré le {maintenant_local().strftime('%d/%m/%Y')} | Document destiné aux professionnels de santé et régulateurs.", styles['Normal']))
    elements.append(Spacer(1, 15))

    headers = ["Pharmacie", "Quartier", "Adresse", "Téléphone", "Statut", "Début Garde", "Fin Garde"]
    data = [headers]

    for _, r in df_export.iterrows():
        d_deb = r["garde_debut"].strftime("%d/%m/%Y") if pd.notna(r["garde_debut"]) else "-"
        d_fin = r["garde_fin"].strftime("%d/%m/%Y") if pd.notna(r["garde_fin"]) else "-"
        data.append([str(r["nom"]), str(r["quartier"]), str(r["adresse"]), str(r["telephone"]), str(r["statut"]).capitalize(), d_deb, d_fin])

    table = Table(data, repeatRows=1)
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1b5e20')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 10),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 6),
        ('BACKGROUND', (0, 1), (-1, -1), colors.HexColor('#f9f9f9')),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#cccccc')),
        ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 1), (-1, -1), 9),
    ]))
    elements.append(table)
    doc.build(elements)
    buffer.seek(0)
    return buffer.getvalue()


def planning_semaine(df: pd.DataFrame, debut: date) -> pd.DataFrame:
    """Une ligne par (jour, pharmacie de garde) sur 7 jours à partir de `debut`."""
    gardes = df[df["statut"] == "garde"]
    lignes = []
    for i in range(7):
        j = debut + timedelta(days=i)
        label = f"{JOURS_FR[j.weekday()]} {j.strftime('%d/%m')}"
        actifs = gardes[masque_actif(gardes, j)]
        if actifs.empty:
            lignes.append({"jour": label, "nom": "Aucune garde répertoriée", "quartier": "", "adresse": "", "telephone": ""})
        else:
            for r in actifs.to_dict("records"):
                lignes.append({"jour": label, "nom": r["nom"], "quartier": r["quartier"], "adresse": r["adresse"], "telephone": r["telephone"]})
    return pd.DataFrame(lignes)


@st.cache_data(show_spinner=False)
def generer_planning_pdf(planning: pd.DataFrame, titre: str) -> bytes:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=24, leftMargin=24, topMargin=24, bottomMargin=24)
    styles = getSampleStyleSheet()
    titre_style = ParagraphStyle("TitrePlanning", parent=styles["Heading1"], fontName="Helvetica-Bold", fontSize=15, textColor=colors.HexColor("#1b5e20"), spaceAfter=6)
    cell = ParagraphStyle("CellPlanning", parent=styles["Normal"], fontSize=9, leading=11)

    elements = [
        Paragraph(f"<b>{xml_escape(titre)}</b>", titre_style),
        Paragraph(f"Généré le {maintenant_local().strftime('%d/%m/%Y à %H:%M')} — consultable sans connexion internet.", styles["Normal"]),
        Spacer(1, 12),
    ]
    data = [["Jour", "Pharmacie", "Quartier", "Adresse", "Téléphone"]]
    for r in planning.itertuples():
        data.append([Paragraph(xml_escape(str(x)), cell) for x in (r.jour, r.nom, r.quartier, r.adresse, r.telephone)])

    table = Table(data, repeatRows=1, colWidths=[70, 130, 80, 160, 90])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1b5e20")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 10),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f1f8e9")]),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    elements.append(table)
    doc.build(elements)
    return buffer.getvalue()


def _charger_police(taille: int, gras: bool = False):
    noms = ["DejaVuSans-Bold.ttf", "arialbd.ttf"] if gras else ["DejaVuSans.ttf", "arial.ttf"]
    for nom in noms:
        try:
            return ImageFont.truetype(nom, taille)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=taille)
    except TypeError:
        return ImageFont.load_default()


def _tronquer(draw, texte: str, police, largeur_max: int) -> str:
    if draw.textlength(texte, font=police) <= largeur_max:
        return texte
    while len(texte) > 1 and draw.textlength(texte + "...", font=police) > largeur_max:
        texte = texte[:-1]
    return texte + "..."


@st.cache_data(show_spinner=False)
def generer_planning_png(planning: pd.DataFrame, titre: str) -> bytes:
    """Image portrait (1080 px) lisible sur téléphone et partageable sur WhatsApp."""
    L, M, H_LIGNE = 1080, 40, 88
    f_titre, f_jour = _charger_police(36, True), _charger_police(30, True)
    f_nom, f_detail, f_pied = _charger_police(30, True), _charger_police(24), _charger_police(22)

    jours = list(dict.fromkeys(planning["jour"]))
    hauteur = 140 + sum(66 + int((planning["jour"] == j).sum()) * H_LIGNE + 8 for j in jours) + 70

    img = Image.new("RGB", (L, hauteur), "white")
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, L, 110], fill="#1b5e20")
    d.text((M, 30), _tronquer(d, titre, f_titre, L - 2 * M), font=f_titre, fill="white")

    y = 130
    for j in jours:
        d.rectangle([M, y, L - M, y + 56], fill="#2e7d32")
        d.text((M + 16, y + 10), j, font=f_jour, fill="white")
        y += 66
        for r in planning[planning["jour"] == j].itertuples():
            d.text((M + 16, y), _tronquer(d, str(r.nom), f_nom, L - 2 * M - 32), font=f_nom, fill="#111111")
            if r.quartier:
                detail = f"{r.quartier} · {r.adresse} · Tél : {r.telephone}"
                d.text((M + 16, y + 40), _tronquer(d, detail, f_detail, L - 2 * M - 32), font=f_detail, fill="#444444")
            d.line([M, y + H_LIGNE - 8, L - M, y + H_LIGNE - 8], fill="#dddddd", width=2)
            y += H_LIGNE
        y += 8

    pied = f"Généré le {maintenant_local().strftime('%d/%m/%Y à %H:%M')} - Pharmacies Brazzaville"
    d.text((M, hauteur - 50), pied, font=f_pied, fill="#666666")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ==========================================
# CHARGEMENT DES DONNÉES (sécurisé)
# ==========================================
df, erreur_donnees = charger_donnees_securise()


def verifier_donnees() -> bool:
    """Affiche un message clair (au lieu d'un plantage) si les données sont indisponibles."""
    if erreur_donnees is None and not df.empty:
        return True
    st.title("💊 Pharmacies de Brazzaville")
    st.error("⚠️ Les données des pharmacies sont momentanément indisponibles.")
    st.write("En cas d'urgence, appelez directement la pharmacie la plus proche ou le numéro d'urgence de votre quartier.")
    with st.expander("Détails techniques (pour l'administrateur)"):
        st.code(erreur_donnees or "Aucune donnée dans la table 'pharmacies'.")
    if st.button("🔄 Réessayer"):
        st.cache_data.clear()
        st.rerun()
    return False


# ==========================================
# PAGE 1 : PHARMACIES & CARTE INTERACTIVE
# ==========================================
def page_principale():
    appliquer_theme_sombre()
    if not verifier_donnees():
        return

    maintenant = maintenant_local()
    maintenant_min = maintenant.replace(second=0)
    user_lat, user_lon = None, None

    with st.sidebar:
        afficher_logo_sidebar()
        st.title("Filtres & Recherche")
        st.caption(f"🕒 Heure locale (Brazzaville) : {maintenant:%H:%M}")
        st.markdown("---")

        autour = st.toggle("📍 Trouver autour de moi")
        if autour:
            loc = streamlit_geolocation()
            if loc and loc.get("latitude") is not None and loc.get("longitude") is not None:
                user_lat, user_lon = float(loc["latitude"]), float(loc["longitude"])
                st.success(f"Position détectée (précision ≈ {int(loc.get('accuracy') or 0)} m)")
            else:
                quartier_ref = st.selectbox("Ou indiquez votre quartier", ["—"] + sorted(df["quartier"].unique().tolist()), key="quartier_ref")
                if quartier_ref != "—":
                    ref = df[df["quartier"] == quartier_ref]
                    user_lat, user_lon = float(ref["latitude"].mean()), float(ref["longitude"].mean())

        st.markdown("---")
        ouvertes_maintenant = st.checkbox("🌙 Ouvertes actuellement", value=False, help="Pharmacies ouvertes à cette minute, selon l'heure de Brazzaville.")
        recherche_nom = st.text_input("🔍 Pharmacie par nom", placeholder="Ex: Pharmacie du Centre...")
        recherche_medoc = st.text_input("💊 Rechercher un médicament", placeholder="Ex: Paracétamol, Insuline...")
        filtre_statut = st.radio("📋 Type de service", options=["Toutes", "De jour", "De garde / nuit"])
        quartiers = ["Tous"] + sorted(df["quartier"].unique().tolist())
        filtre_quartier = st.selectbox("📍 Quartier", quartiers)
        only_active = st.checkbox("🟢 Actives aujourd'hui", value=True)

    st.title("💊 Pharmacies de Brazzaville")
    st.caption("Recherche géolocalisée et mise à jour collaborative en temps réel.")

    resultat = rechercher_pharmacies(
        df,
        filtre_statut,
        filtre_quartier,
        recherche_nom.strip(),
        recherche_medoc.strip(),
        only_active,
        ouvertes_maintenant,
        user_lat,
        user_lon,
        maintenant_min,
    )
    verifs = charger_verifications()
    lignes = resultat.to_dict("records")

    st.markdown(f"### **{len(lignes)}** pharmacie(s) trouvée(s)")

    centre_lat = resultat["latitude"].mean() if len(resultat) > 0 else -4.2634
    centre_lon = resultat["longitude"].mean() if len(resultat) > 0 else 15.2429
    if user_lat is not None:
        centre_lat, centre_lon = user_lat, user_lon

    if len(lignes) > SEUIL_CLUSTER:
        st.caption(f"🗺️ Mode regroupement activé ({len(lignes)} pharmacies) : zoomez pour séparer les marqueurs.")

    carte = construire_carte(lignes, user_lat, user_lon, centre_lat, centre_lon)
    # returned_objects=[] : évite de relancer toute l'app à chaque déplacement/zoom sur la carte
    st_folium(carte, width=None, height=450, returned_objects=[])

    st.subheader("Liste des pharmacies")
    st.session_state.setdefault("nb_liste", LIMITE_LISTE)
    nb_liste = st.session_state["nb_liste"]

    for idx, row in enumerate(lignes[:nb_liste]):
        type_label = "☀️ Pharmacie de jour" if row["statut"] == "jour" else "🟠 Pharmacie de garde"
        texte_verif, couleur_verif = badge_verification(verifs.get(row["nom"]), maintenant)

        gmaps_url = f"https://www.google.com/maps/dir/?api=1&destination={row['latitude']},{row['longitude']}"
        tel_clean = str(row["telephone"]).replace(" ", "").replace("-", "")
        meds_list = [med.strip() for med in str(row["stock_medicaments"]).split(",") if med.strip()]
        meds_badges = "".join([f'<span class="med-badge">💊 {html.escape(med)}</span>' for med in meds_list])
        
        with st.container(border=True):
            c1, c2 = st.columns([3, 2])
            with c1:
                st.markdown(f"**{row['nom']}** — *{row['quartier']}*")
                st.caption(f"📍 {row['adresse']} · 📞 {row['telephone']}")
                st.markdown(f"**Stocks :** {meds_badges}", unsafe_allow_html=True)
                st.markdown(f'<span style="color:{couleur_verif}; font-size:12px;">{texte_verif}</span>', unsafe_allow_html=True)
                
                st.markdown(
                    f"""
                    <div style="margin-top: 8px; margin-bottom: 8px;">
                        <a href="tel:{tel_clean}" class="btn-action btn-call">📞 Appeler</a>
                        <a href="{gmaps_url}" class="btn-action btn-maps" target="_blank">🗺️ Itinéraire</a>
                    </div>
                    """,
                    unsafe_allow_html=True
                )
                
                if st.button("🚨 Signaler une erreur", key=f"sig_{idx}"):
                    dialog_signalement(row["nom"])

            with c2:
                st.markdown(etat_ouverture(row))
                st.caption(f"{type_label} · 🕗 {horaires_label(row)}")
                if pd.notna(row["distance_km"]):
                    st.markdown(f"📏 **{row['distance_km']:.1f} km**")

    if len(lignes) > nb_liste:
        if st.button(f"⬇️ Afficher plus ({len(lignes) - nb_liste} restantes)"):
            st.session_state["nb_liste"] = nb_liste + LIMITE_LISTE
            st.rerun()

# ==========================================
# PAGE 2 : CALENDRIER PRÉVISIONNEL DE GARDE
# ==========================================
def page_calendrier():
    appliquer_theme_sombre()
    if not verifier_donnees():
        return

    st.title("📅 Calendrier prévisionnel des gardes")
    st.caption("Consultez l'agenda dynamique des gardes pour le week-end prochain ou à des dates ultérieures.")

    today = maintenant_local().date()

    with st.sidebar:
        afficher_logo_sidebar()
        st.title("Options du Calendrier")
        st.markdown("---")
        
        mode_filtre = st.radio("📆 Période souhaitée", options=["Date spécifique", "Week-end prochain", "Ce mois-ci"], index=1)
        st.markdown("---")
        
        if mode_filtre == "Date spécifique":
            date_selectionnee = st.date_input("Choisir une date", value=today, min_value=today)
        elif mode_filtre == "Week-end prochain":
            jours_jusqu_au_samedi = (5 - today.weekday()) % 7
            samedi_prochain = today if jours_jusqu_au_samedi == 0 else today + timedelta(days=jours_jusqu_au_samedi)
            date_selectionnee = samedi_prochain
            st.info(f"Week-end du **{samedi_prochain.strftime('%d/%m/%Y')}** au **{(samedi_prochain + timedelta(days=1)).strftime('%d/%m/%Y')}**")
        else:
            date_selectionnee = today

    df_garde_seule = df[df["statut"] == "garde"].copy()

    if mode_filtre == "Date spécifique":
        st.subheader(f"Pharmacies de garde le **{date_selectionnee.strftime('%d/%m/%Y')}**")
        gardes_du_jour = df_garde_seule[masque_actif(df_garde_seule, date_selectionnee)]

        if len(gardes_du_jour) > 0:
            for _, row in gardes_du_jour.iterrows():
                with st.container(border=True):
                    c1, c2 = st.columns([3, 1])
                    with c1:
                        st.markdown(f"### **{row['nom']}** ({row['quartier']})")
                        st.caption(f"📍 {row['adresse']} · 📞 {row['telephone']}")
                        if pd.notna(row["garde_debut"]) and pd.notna(row["garde_fin"]):
                            st.write(f"🗓️ Période de garde : du **{row['garde_debut'].strftime('%d/%m/%Y')}** au **{row['garde_fin'].strftime('%d/%m/%Y')}**")
                    with c2:
                        st.success("🟠 DE GARDE")
        else:
            st.warning(f"Aucune pharmacie de garde répertoriée pour la date du {date_selectionnee.strftime('%d/%m/%Y')}.")

    elif mode_filtre == "Week-end prochain":
        samedi = date_selectionnee
        dimanche = samedi + timedelta(days=1)
        st.subheader(f"Garde du Week-end : du **{samedi.strftime('%d/%m/%Y')}** au **{dimanche.strftime('%d/%m/%Y')}**")

        masque_we = masque_actif(df_garde_seule, samedi) | masque_actif(df_garde_seule, dimanche)
        gardes_we = df_garde_seule[masque_we]

        if len(gardes_we) > 0:
            for _, row in gardes_we.iterrows():
                with st.container(border=True):
                    st.markdown(f"### 💊 **{row['nom']}** — quartier *{row['quartier']}*")
                    st.caption(f"📍 {row['adresse']} | 📞 {row['telephone']}")
                    if pd.notna(row["garde_debut"]) and pd.notna(row["garde_fin"]):
                        st.info(f"🗓️ Tour de garde : du {row['garde_debut'].strftime('%d/%m/%Y')} au {row['garde_fin'].strftime('%d/%m/%Y')}")
        else:
            st.warning("Aucune garde programmée pour le week-end prochain.")

    else:
        st.subheader(f"Planning complet du mois de **{today.strftime('%B %Y').capitalize()}**")
        mois_actuel = today.month
        gardes_mois = df_garde_seule[(df_garde_seule["garde_debut"].dt.month == mois_actuel) | (df_garde_seule["garde_fin"].dt.month == mois_actuel)].sort_values("garde_debut")

        if len(gardes_mois) > 0:
            df_display = gardes_mois[["nom", "quartier", "telephone", "garde_debut", "garde_fin"]].copy()
            df_display["garde_debut"] = df_display["garde_debut"].dt.strftime("%d/%m/%Y")
            df_display["garde_fin"] = df_display["garde_fin"].dt.strftime("%d/%m/%Y")
            df_display.columns = ["Pharmacie", "Quartier", "Téléphone", "Début de garde", "Fin de garde"]
            st.dataframe(df_display, use_container_width=True, hide_index=True)
        else:
            st.info("Aucun tour de garde enregistré pour ce mois-ci.")

    # --- Planning de la semaine, utilisable hors connexion ---
    st.markdown("---")
    st.subheader("📲 Planning de la semaine — à consulter sans connexion")
    fin_semaine = today + timedelta(days=6)
    st.caption(
        f"Du {today.strftime('%d/%m/%Y')} au {fin_semaine.strftime('%d/%m/%Y')}. "
        "Téléchargez-le une fois : il reste dans votre téléphone et s'ouvre sans internet "
        "(l'image se partage facilement sur WhatsApp)."
    )
    planning = planning_semaine(df, today)
    titre_planning = f"Pharmacies de garde - du {today.strftime('%d/%m')} au {fin_semaine.strftime('%d/%m/%Y')}"
    nom_fichier = f"planning_gardes_{today.strftime('%Y%m%d')}"

    col_img, col_pdf = st.columns(2)
    with col_img:
        st.download_button(
            "🖼️ Télécharger en image (PNG)",
            data=generer_planning_png(planning, titre_planning),
            file_name=f"{nom_fichier}.png",
            mime="image/png",
            use_container_width=True,
        )
    with col_pdf:
        if REPORTLAB_AVAILABLE:
            st.download_button(
                "📄 Télécharger en PDF",
                data=generer_planning_pdf(planning, titre_planning),
                file_name=f"{nom_fichier}.pdf",
                mime="application/pdf",
                use_container_width=True,
            )
        else:
            st.info("Installez 'reportlab' pour activer le PDF.")

# ==========================================
# PAGE 3 : EXPORTATION & RAPPORTS
# ==========================================
def page_export():
    appliquer_theme_sombre()
    if not verifier_donnees():
        return

    st.title("📥 Exportation & Rapports Officiels")
    st.caption("Outil réservé aux professionnels de santé, régulateurs et autorités de santé publique.")

    with st.sidebar:
        afficher_logo_sidebar()
        st.title("Filtres d'export")
        st.markdown("---")

        filtre_statut_exp = st.selectbox("Statut", ["Toutes", "De garde uniquement", "De jour uniquement"])
        quartiers_exp = ["Tous"] + sorted(df["quartier"].unique().tolist())
        filtre_quartier_exp = st.selectbox("Quartier", quartiers_exp, key="export_q")

    df_exp = df.copy()

    if filtre_statut_exp == "De garde uniquement":
        df_exp = df_exp[df_exp["statut"] == "garde"]
    elif filtre_statut_exp == "De jour uniquement":
        df_exp = df_exp[df_exp["statut"] == "jour"]

    if filtre_quartier_exp != "Tous":
        df_exp = df_exp[df_exp["quartier"] == filtre_quartier_exp]

    st.markdown(f"### **{len(df_exp)}** enregistrement(s) sélectionné(s)")

    df_preview = df_exp[["nom", "quartier", "adresse", "telephone", "statut", "garde_debut", "garde_fin"]].copy()
    df_preview["garde_debut"] = df_preview["garde_debut"].dt.strftime("%d/%m/%Y")
    df_preview["garde_fin"] = df_preview["garde_fin"].dt.strftime("%d/%m/%Y")
    df_preview.columns = ["Pharmacie", "Quartier", "Adresse", "Téléphone", "Statut", "Début Garde", "Fin Garde"]
    
    st.dataframe(df_preview, use_container_width=True, hide_index=True)

    st.markdown("---")
    st.subheader("Télécharger le rapport")

    col_exp1, col_exp2 = st.columns(2)
    aujourdhui_txt = maintenant_local().strftime("%Y%m%d")

    with col_exp1:
        buffer_excel = io.BytesIO()
        with pd.ExcelWriter(buffer_excel, engine="openpyxl") as writer:
            df_preview.to_excel(writer, index=False, sheet_name="Pharmacies_Garde")
        buffer_excel.seek(0)

        st.download_button(
            label="📊 Télécharger au format Excel (.xlsx)",
            data=buffer_excel,
            file_name=f"tours_de_garde_brazzaville_{aujourdhui_txt}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True
        )

    with col_exp2:
        if REPORTLAB_AVAILABLE:
            pdf_bytes = generer_pdf(df_exp)
            st.download_button(
                label="📄 Télécharger le rapport officiel PDF (.pdf)",
                data=pdf_bytes,
                file_name=f"tours_de_garde_brazzaville_{aujourdhui_txt}.pdf",
                mime="application/pdf",
                use_container_width=True
            )
        else:
            st.error("Installez 'reportlab' (`pip install reportlab`) pour activer l'export PDF.")

# ==========================================
# PAGE 4 : ADMINISTRATION (validation des signalements)
# ==========================================
def _mot_de_passe_admin():
    try:
        mdp = st.secrets["ADMIN_PASSWORD"]
    except Exception:
        mdp = os.environ.get("ADMIN_PASSWORD")
    return str(mdp) if mdp else None


def page_admin():
    appliquer_theme_sombre()
    st.title("🔐 Administration des signalements")

    mdp = _mot_de_passe_admin()
    if not mdp:
        st.warning(
            "Page désactivée : aucun mot de passe administrateur n'est configuré. "
            "Ajoutez `ADMIN_PASSWORD` dans les *secrets* Streamlit (jamais dans GitHub)."
        )
        return

    if not st.session_state.get("admin_ok"):
        saisie = st.text_input("Mot de passe administrateur", type="password")
        if st.button("Se connecter", type="primary"):
            if hmac.compare_digest(saisie.encode(), mdp.encode()):
                st.session_state["admin_ok"] = True
                st.rerun()
            else:
                st.error("Mot de passe incorrect.")
        return

    if st.button("Se déconnecter"):
        st.session_state["admin_ok"] = False
        st.rerun()

    st.subheader("Marquer une pharmacie comme vérifiée")
    if not df.empty:
        nom_verif = st.selectbox("Pharmacie", sorted(df["nom"].unique().tolist()))
        if st.button("✅ Confirmer la vérification d'aujourd'hui"):
            marquer_pharmacie_verifiee(nom_verif)
            st.success(f"« {nom_verif} » est marquée comme vérifiée.")

    st.markdown("---")
    en_attente = lire_signalements(en_attente=True)
    st.subheader(f"Signalements en attente ({len(en_attente)})")
    if en_attente.empty:
        st.info("Aucun signalement à traiter.")
    for r in en_attente.to_dict("records"):
        with st.container(border=True):
            st.markdown(f"**{r['pharmacie']}** — {r['type_erreur']}")
            st.caption(f"Reçu le {r['date_signalement']} · Contact : {r.get('contact_citoyen') or '—'}")
            st.write(r["description"])
            c1, c2 = st.columns(2)
            if c1.button("✅ Valider", key=f"val_{r['id']}"):
                changer_statut_signalement(int(r["id"]), "valide")
                st.rerun()
            if c2.button("❌ Rejeter", key=f"rej_{r['id']}"):
                changer_statut_signalement(int(r["id"]), "rejete")
                st.rerun()

    with st.expander("Derniers signalements traités"):
        traites = lire_signalements(en_attente=False)
        st.dataframe(traites, use_container_width=True, hide_index=True)

# ==========================================
# NAVIGATION MULTI-PAGES
# ==========================================
pg_pharmacies = st.Page(page_principale, title="Pharmacies & Géolocalisation", icon="💊")
pg_calendrier = st.Page(page_calendrier, title="Agenda des Gardes", icon="📅")
pg_export = st.Page(page_export, title="Export & Rapports", icon="📥")
pg_admin = st.Page(page_admin, title="Administration", icon="🔐")

pg = st.navigation([pg_pharmacies, pg_calendrier, pg_export, pg_admin])
pg.run()

# ==========================================
# FOOTER COMMUN
# ==========================================
footer_html = """
<div class="footer">
    <p>💊 <b>Pharmacies Brazzaville</b> © 2026 — Données de santé de proximité | Pour mettre à jour la garde ou les stocks</p>
</div>
"""
st.markdown(footer_html, unsafe_allow_html=True)
