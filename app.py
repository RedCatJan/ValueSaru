import streamlit as st
import requests
import gspread
import pandas as pd

from google.oauth2.service_account import Credentials
from datetime import datetime, timezone
from io import BytesIO


# ============================================================
# CONFIGURATION
# ============================================================

st.set_page_config(
    page_title="Value Scanner",
    page_icon="📊",
    layout="wide"
)

# Seuil officiel figé
EDGE_MIN = 0.05

SPORTS = {
    "Japon J-League": "soccer_japan_j_league",
    "MLS": "soccer_usa_mls",
    "Mexique Liga MX": "soccer_mexico_ligamx",
    "Brésil Serie A": "soccer_brazil_campeonato",
    "Argentine Primera": "soccer_argentina_primera_division",
    "Norvège Eliteserien": "soccer_norway_eliteserien",
    "Suède Allsvenskan": "soccer_sweden_allsvenskan",
}

API_KEY = st.secrets["ODDS_API_KEY"]
GOOGLE_SHEET_ID = st.secrets["GOOGLE_SHEET_ID"]


# ============================================================
# GOOGLE SHEETS
# ============================================================

@st.cache_resource
def connexion_google():

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]

    credentials = Credentials.from_service_account_info(
        dict(st.secrets["gcp_service_account"]),
        scopes=scopes
    )

    client = gspread.authorize(credentials)

    return client.open_by_key(GOOGLE_SHEET_ID)


spreadsheet = connexion_google()

sheet_scans = spreadsheet.worksheet("Scans")
sheet_matchs = spreadsheet.worksheet("Matchs")
sheet_signals = spreadsheet.worksheet("Signals")


# ============================================================
# HEADERS GOOGLE SHEETS
# ============================================================

MATCHS_HEADERS = [
    "scan_id",
    "timestamp",
    "league",
    "home",
    "away",
    "match_time",
    "minutes_before_match",
    "timing",
    "outcome",

    # Anciennes colonnes conservées
    "fair_odd",
    "best_odd",
    "bookmaker",
    "edge_pct",
    "signal",
    "bookmakers_used",

    # Nouvelles colonnes historiques
    "fair_odd_historical",
    "edge_historical_pct",
    "signal_historical"
]


def verifier_headers_matchs():

    first_row = sheet_matchs.row_values(1)

    # Si certaines nouvelles colonnes manquent,
    # on met à jour uniquement la ligne d'en-tête.
    if first_row != MATCHS_HEADERS:
        sheet_matchs.update(
            "A1:R1",
            [MATCHS_HEADERS]
        )


verifier_headers_matchs()


# ============================================================
# MÉTHODE V5
# Démarge bookmaker par bookmaker
# ============================================================

def demarger(c1, cx, c2):

    p1 = 1 / c1
    px = 1 / cx
    p2 = 1 / c2

    total = p1 + px + p2

    return (
        p1 / total,
        px / total,
        p2 / total
    )


# ============================================================
# ANALYSE D'UN MATCH
# ============================================================

def analyser_match(match, league_name, scan_id):

    home = match["home_team"]
    away = match["away_team"]

    commence_time = match["commence_time"]

    match_time = datetime.fromisoformat(
        commence_time.replace("Z", "+00:00")
    )

    now = datetime.now(timezone.utc)

    minutes_before_match = (
        match_time - now
    ).total_seconds() / 60

    # Match déjà commencé
    if minutes_before_match <= 0:
        return []

    if minutes_before_match <= 60:
        timing = "CLOSING"
    else:
        timing = "EARLY"

    # ========================================================
    # Données pour méthode V5
    # ========================================================

    bookmaker_probs = []

    # ========================================================
    # Données pour méthode historique
    # Moyenne brute des cotes
    # ========================================================

    raw_odds_home = []
    raw_odds_draw = []
    raw_odds_away = []

    # ========================================================
    # Meilleures cotes
    # ========================================================

    best = {
        home: {
            "odd": 0,
            "book": None
        },
        "Draw": {
            "odd": 0,
            "book": None
        },
        away: {
            "odd": 0,
            "book": None
        }
    }

    bookmakers_used = 0

    # ========================================================
    # BOUCLE BOOKMAKERS
    # ========================================================

    for bookmaker in match.get("bookmakers", []):

        book_name = bookmaker.get(
            "title",
            "Inconnu"
        )

        for market in bookmaker.get("markets", []):

            if market.get("key") != "h2h":
                continue

            odds = {}

            for outcome in market.get("outcomes", []):
                odds[outcome["name"]] = outcome["price"]

            if home not in odds:
                continue

            if away not in odds:
                continue

            draw_key = None

            for name in odds:
                if name.lower() == "draw":
                    draw_key = name
                    break

            if draw_key is None:
                continue

            c1 = float(odds[home])
            cx = float(odds[draw_key])
            c2 = float(odds[away])

            if c1 <= 1 or cx <= 1 or c2 <= 1:
                continue

            # ------------------------------------------------
            # MÉTHODE HISTORIQUE :
            # stocker les cotes brutes
            # ------------------------------------------------

            raw_odds_home.append(c1)
            raw_odds_draw.append(cx)
            raw_odds_away.append(c2)

            # ------------------------------------------------
            # MÉTHODE V5 :
            # démarge chaque bookmaker individuellement
            # ------------------------------------------------

            p1, px, p2 = demarger(
                c1,
                cx,
                c2
            )

            bookmaker_probs.append(
                (p1, px, p2)
            )

            bookmakers_used += 1

            # ------------------------------------------------
            # Meilleures cotes
            # ------------------------------------------------

            if c1 > best[home]["odd"]:
                best[home] = {
                    "odd": c1,
                    "book": book_name
                }

            if cx > best["Draw"]["odd"]:
                best["Draw"] = {
                    "odd": cx,
                    "book": book_name
                }

            if c2 > best[away]["odd"]:
                best[away] = {
                    "odd": c2,
                    "book": book_name
                }

    # Pas assez de données
    if not bookmaker_probs:
        return []

    if (
        not raw_odds_home
        or not raw_odds_draw
        or not raw_odds_away
    ):
        return []

    # ========================================================
    # MÉTHODE V5
    # Moyenne des probabilités démargées
    # ========================================================

    consensus_home_v5 = (
        sum(x[0] for x in bookmaker_probs)
        / len(bookmaker_probs)
    )

    consensus_draw_v5 = (
        sum(x[1] for x in bookmaker_probs)
        / len(bookmaker_probs)
    )

    consensus_away_v5 = (
        sum(x[2] for x in bookmaker_probs)
        / len(bookmaker_probs)
    )

    fair_v5 = {
        home: 1 / consensus_home_v5,
        "Draw": 1 / consensus_draw_v5,
        away: 1 / consensus_away_v5
    }

    # ========================================================
    # MÉTHODE HISTORIQUE
    #
    # 1. moyenne brute des cotes
    # 2. probabilités implicites
    # 3. retrait de marge
    # 4. fair odds
    #
    # C'est cette méthode qui doit être comparée
    # au backtest Japon.
    # ========================================================

    avg_home = (
        sum(raw_odds_home)
        / len(raw_odds_home)
    )

    avg_draw = (
        sum(raw_odds_draw)
        / len(raw_odds_draw)
    )

    avg_away = (
        sum(raw_odds_away)
        / len(raw_odds_away)
    )

    p_home_raw = 1 / avg_home
    p_draw_raw = 1 / avg_draw
    p_away_raw = 1 / avg_away

    total_raw = (
        p_home_raw
        + p_draw_raw
        + p_away_raw
    )

    hist_prob_home = (
        p_home_raw / total_raw
    )

    hist_prob_draw = (
        p_draw_raw / total_raw
    )

    hist_prob_away = (
        p_away_raw / total_raw
    )

    fair_historical = {
        home: 1 / hist_prob_home,
        "Draw": 1 / hist_prob_draw,
        away: 1 / hist_prob_away
    }

    # ========================================================
    # CONSTRUCTION DES 3 ISSUES
    # ========================================================

    rows = []

    timestamp = datetime.now(
        timezone.utc
    ).isoformat(timespec="seconds")

    for outcome in [
        home,
        "Draw",
        away
    ]:

        max_odd = best[outcome]["odd"]

        if max_odd == 0:
            continue

        # ----------------------------------------------------
        # Fair odds
        # ----------------------------------------------------

        fair_odd_v5 = fair_v5[outcome]

        fair_odd_hist = (
            fair_historical[outcome]
        )

        # ----------------------------------------------------
        # Edge V5
        # ----------------------------------------------------

        edge_v5 = (
            max_odd / fair_odd_v5
        ) - 1

        # ----------------------------------------------------
        # Edge historique
        # ----------------------------------------------------

        edge_historical = (
            max_odd / fair_odd_hist
        ) - 1

        signal_v5 = (
            edge_v5 >= EDGE_MIN
        )

        signal_historical = (
            edge_historical >= EDGE_MIN
        )

        rows.append({

            "scan_id": scan_id,

            "timestamp": timestamp,

            "league": league_name,

            "home": home,

            "away": away,

            "match_time": commence_time,

            "minutes_before_match":
                round(
                    minutes_before_match,
                    1
                ),

            "timing": timing,

            "outcome": outcome,

            # -----------------------------------------------
            # Anciennes colonnes :
            # représentent désormais la méthode V5
            # -----------------------------------------------

            "fair_odd":
                round(
                    fair_odd_v5,
                    4
                ),

            "best_odd":
                round(
                    max_odd,
                    4
                ),

            "bookmaker":
                best[outcome]["book"],

            "edge_pct":
                round(
                    edge_v5 * 100,
                    2
                ),

            "signal":
                "YES"
                if signal_v5
                else "NO",

            "bookmakers_used":
                bookmakers_used,

            # -----------------------------------------------
            # Méthode historique
            # -----------------------------------------------

            "fair_odd_historical":
                round(
                    fair_odd_hist,
                    4
                ),

            "edge_historical_pct":
                round(
                    edge_historical * 100,
                    2
                ),

            "signal_historical":
                "YES"
                if signal_historical
                else "NO"
        })

    return rows


# ============================================================
# SCANNER UNE LIGUE
# ============================================================

def scanner_ligue(
    league_name,
    sport_key,
    scan_id
):

    url = (
        "https://api.the-odds-api.com/"
        f"v4/sports/{sport_key}/odds"
    )

    params = {
        "apiKey": API_KEY,
        "regions": "fr",
        "markets": "h2h",
        "oddsFormat": "decimal"
    }

    response = requests.get(
        url,
        params=params,
        timeout=20
    )

    remaining = response.headers.get(
        "x-requests-remaining"
    )

    used = response.headers.get(
        "x-requests-used"
    )

    last = response.headers.get(
        "x-requests-last"
    )

    if response.status_code != 200:

        return {
            "rows": [],
            "matches": 0,
            "remaining": remaining,
            "used": used,
            "last": last,
            "error": response.text
        }

    matches = response.json()

    rows = []

    for match in matches:

        rows.extend(
            analyser_match(
                match,
                league_name,
                scan_id
            )
        )

    return {
        "rows": rows,
        "matches": len(matches),
        "remaining": remaining,
        "used": used,
        "last": last,
        "error": None
    }


# ============================================================
# ENREGISTRER TOUS LES MATCHS
# ============================================================

def enregistrer_matchs(rows):

    if not rows:
        return

    data = []

    for r in rows:

        data.append([

            r["scan_id"],
            r["timestamp"],
            r["league"],
            r["home"],
            r["away"],
            r["match_time"],
            r["minutes_before_match"],
            r["timing"],
            r["outcome"],

            # V5
            r["fair_odd"],
            r["best_odd"],
            r["bookmaker"],
            r["edge_pct"],
            r["signal"],
            r["bookmakers_used"],

            # Historique
            r["fair_odd_historical"],
            r["edge_historical_pct"],
            r["signal_historical"]
        ])

    sheet_matchs.append_rows(
        data,
        value_input_option="USER_ENTERED"
    )


# ============================================================
# ANTI-DOUBLONS SIGNALS
# ============================================================

def recuperer_signal_keys():

    values = sheet_signals.get_all_records()

    keys = set()

    for row in values:

        key = (
            f"{row.get('league')}|"
            f"{row.get('home')}|"
            f"{row.get('away')}|"
            f"{row.get('match_time')}|"
            f"{row.get('outcome')}|"
            f"{row.get('timing')}"
        )

        keys.add(key)

    return keys


# ============================================================
# ENREGISTRER SIGNALS HISTORIQUES
# ============================================================

def enregistrer_signals(rows):

    # IMPORTANT :
    # seuls les signaux de la formule historique
    # sont enregistrés comme signaux officiels.

    signals = [
        x
        for x in rows
        if x["signal_historical"] == "YES"
    ]

    if not signals:
        return 0

    existing_keys = recuperer_signal_keys()

    new_rows = []

    for r in signals:

        key = (
            f"{r['league']}|"
            f"{r['home']}|"
            f"{r['away']}|"
            f"{r['match_time']}|"
            f"{r['outcome']}|"
            f"{r['timing']}"
        )

        if key in existing_keys:
            continue

        existing_keys.add(key)

        new_rows.append([

            r["scan_id"],
            r["timestamp"],
            r["league"],
            r["home"],
            r["away"],
            r["match_time"],
            r["minutes_before_match"],
            r["timing"],
            r["outcome"],

            # Ici on stocke la méthode historique
            r["fair_odd_historical"],
            r["best_odd"],
            r["bookmaker"],
            r["edge_historical_pct"],

            "",  # result
            "",  # closing_odd
            "",  # clv_pct
            ""   # profit
        ])

    if new_rows:

        sheet_signals.append_rows(
            new_rows,
            value_input_option="USER_ENTERED"
        )

    return len(new_rows)


# ============================================================
# EXPORT EXCEL
# ============================================================

def creer_excel():

    scans = pd.DataFrame(
        sheet_scans.get_all_records()
    )

    matchs = pd.DataFrame(
        sheet_matchs.get_all_records()
    )

    signals = pd.DataFrame(
        sheet_signals.get_all_records()
    )

    buffer = BytesIO()

    with pd.ExcelWriter(
        buffer,
        engine="openpyxl"
    ) as writer:

        scans.to_excel(
            writer,
            sheet_name="Scans",
            index=False
        )

        matchs.to_excel(
            writer,
            sheet_name="Matchs",
            index=False
        )

        signals.to_excel(
            writer,
            sheet_name="Signals",
            index=False
        )

    buffer.seek(0)

    return buffer


# ============================================================
# INTERFACE
# ============================================================

st.title("📊 Value Scanner V6")

st.caption(
    "Deux méthodes en parallèle : "
    "V5 + formule historique du backtest"
)

st.info(
    "🎯 Validation officielle : "
    "CLOSING + edge historique ≥ 5 %"
)


# ============================================================
# CHOIX DES LIGUES
# ============================================================

selected_leagues = st.multiselect(
    "Ligues à scanner",
    list(SPORTS.keys()),
    default=list(SPORTS.keys())
)


# ============================================================
# BOUTON SCAN
# ============================================================

if st.button(
    "🔍 Scanner maintenant",
    use_container_width=True
):

    if not selected_leagues:
        st.warning(
            "Sélectionne au moins une ligue."
        )

    else:

        scan_time = datetime.now(
            timezone.utc
        )

        scan_id = (
            "SCAN-"
            + scan_time.strftime(
                "%Y%m%d-%H%M%S"
            )
        )

        all_rows = []

        total_matches = 0

        credits_after = None

        credits_used_scan = 0

        # ====================================================
        # SCAN
        # ====================================================

        with st.spinner(
            "Analyse en cours..."
        ):

            for league_name in selected_leagues:

                result = scanner_ligue(
                    league_name,
                    SPORTS[league_name],
                    scan_id
                )

                if result["error"]:

                    st.warning(
                        f"{league_name} : "
                        "erreur API"
                    )

                    continue

                total_matches += (
                    result["matches"]
                )

                all_rows.extend(
                    result["rows"]
                )

                # Coût exact de cet appel
                try:
                    credits_used_scan += int(
                        result["last"]
                    )
                except:
                    pass

                try:
                    credits_after = int(
                        result["remaining"]
                    )
                except:
                    pass

        # ====================================================
        # CRÉDITS
        # ====================================================

        credits_before = None

        if credits_after is not None:

            credits_before = (
                credits_after
                + credits_used_scan
            )

        # ====================================================
        # SIGNALS
        # ====================================================

        signal_v5_count = sum(
            1
            for r in all_rows
            if r["signal"] == "YES"
        )

        signal_hist_count = sum(
            1
            for r in all_rows
            if r["signal_historical"] == "YES"
        )

        closing_hist_count = sum(
            1
            for r in all_rows
            if (
                r["signal_historical"] == "YES"
                and r["timing"] == "CLOSING"
            )
        )

        issues_count = len(all_rows)

        # ====================================================
        # SAUVEGARDE GOOGLE
        # ====================================================

        enregistrer_matchs(
            all_rows
        )

        new_signals = enregistrer_signals(
            all_rows
        )

        # ====================================================
        # SCANS SHEET
        # ====================================================

        sheet_scans.append_row([

            scan_id,

            scan_time.isoformat(
                timespec="seconds"
            ),

            len(selected_leagues),

            total_matches,

            issues_count,

            signal_hist_count,

            credits_before
            if credits_before is not None
            else "",

            credits_used_scan,

            credits_after
            if credits_after is not None
            else ""
        ])

        # ====================================================
        # RÉSUMÉ
        # ====================================================

        st.success(
            "Scan terminé"
        )

        c1, c2, c3, c4 = st.columns(4)

        c1.metric(
            "Matchs scannés",
            total_matches
        )

        c2.metric(
            "Issues analysées",
            issues_count
        )

        c3.metric(
            "Signals historiques ≥5%",
            signal_hist_count
        )

        c4.metric(
            "Closing signals",
            closing_hist_count
        )

        # ====================================================
        # CRÉDITS
        # ====================================================

        st.subheader(
            "💳 Crédits API"
        )

        c1, c2, c3 = st.columns(3)

        c1.metric(
            "Avant scan",
            credits_before
            if credits_before is not None
            else "?"
        )

        c2.metric(
            "Utilisés",
            credits_used_scan
        )

        c3.metric(
            "Restants",
            credits_after
            if credits_after is not None
            else "?"
        )

        if (
            credits_after is not None
            and credits_used_scan > 0
        ):

            estimated_scans = (
                credits_after
                // credits_used_scan
            )

            st.write(
                f"Environ **{estimated_scans} scans "
                f"similaires** encore possibles."
            )

        # ====================================================
        # COMPARAISON DES MÉTHODES
        # ====================================================

        st.subheader(
            "🧪 Comparaison des méthodes"
        )

        c1, c2 = st.columns(2)

        c1.metric(
            "Signals V5",
            signal_v5_count
        )

        c2.metric(
            "Signals historiques",
            signal_hist_count
        )

        # ====================================================
        # DÉTAIL COMPLET
        # ====================================================

        st.subheader(
            "📋 Détail du scan"
        )

        df = pd.DataFrame(
            all_rows
        )

        if not df.empty:

            display_columns = [

                "league",
                "home",
                "away",
                "match_time",
                "minutes_before_match",
                "timing",
                "outcome",

                "fair_odd",
                "fair_odd_historical",

                "best_odd",
                "bookmaker",

                "edge_pct",
                "edge_historical_pct",

                "signal",
                "signal_historical",

                "bookmakers_used"
            ]

            st.dataframe(
                df[display_columns],
                use_container_width=True,
                hide_index=True
            )

        # ====================================================
        # VALUES HISTORIQUES
        # ====================================================

        st.subheader(
            "🚨 Values historiques ≥5 %"
        )

        if df.empty:

            st.info(
                "Aucune donnée."
            )

        else:

            signals_df = df[
                df["signal_historical"] == "YES"
            ].copy()

            if signals_df.empty:

                st.info(
                    "Aucune value historique ≥5 %."
                )

            else:

                st.dataframe(
                    signals_df,
                    use_container_width=True,
                    hide_index=True
                )

        # ====================================================
        # CLOSING OFFICIEL
        # ====================================================

        st.subheader(
            "🎯 Closing signals officiels"
        )

        if not df.empty:

            closing_df = df[
                (
                    df["signal_historical"] == "YES"
                )
                &
                (
                    df["timing"] == "CLOSING"
                )
            ].copy()

            if closing_df.empty:

                st.info(
                    "Aucun signal CLOSING historique ≥5 %."
                )

            else:

                st.dataframe(
                    closing_df,
                    use_container_width=True,
                    hide_index=True
                )


# ============================================================
# HISTORIQUE
# ============================================================

st.divider()

st.header(
    "📊 Historique"
)

if st.button(
    "Actualiser l'historique"
):

    scans_df = pd.DataFrame(
        sheet_scans.get_all_records()
    )

    signals_df = pd.DataFrame(
        sheet_signals.get_all_records()
    )

    st.subheader(
        "Scans"
    )

    st.dataframe(
        scans_df,
        use_container_width=True,
        hide_index=True
    )

    st.subheader(
        "Signals historiques"
    )

    st.dataframe(
        signals_df,
        use_container_width=True,
        hide_index=True
    )


# ============================================================
# EXPORT EXCEL
# ============================================================

st.divider()

st.header(
    "📥 Export"
)

try:

    excel_file = creer_excel()

    st.download_button(

        label=(
            "Télécharger l'historique Excel"
        ),

        data=excel_file,

        file_name=(
            "value_scanner_historique.xlsx"
        ),

        mime=(
            "application/vnd.openxmlformats-"
            "officedocument.spreadsheetml.sheet"
        )
    )

except Exception as e:

    st.warning(
        "Export Excel temporairement indisponible."
    )
