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
# CALCUL FAIR ODDS
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

    if minutes_before_match <= 0:
        return []

    if minutes_before_match <= 60:
        timing = "CLOSING"
    else:
        timing = "EARLY"

    bookmaker_probs = []

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

    for bookmaker in match.get("bookmakers", []):

        book_name = bookmaker.get(
            "title",
            "Inconnu"
        )

        for market in bookmaker.get(
            "markets",
            []
        ):

            if market.get("key") != "h2h":
                continue

            odds = {}

            for outcome in market.get(
                "outcomes",
                []
            ):

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

            c1 = odds[home]
            cx = odds[draw_key]
            c2 = odds[away]

            p1, px, p2 = demarger(
                c1,
                cx,
                c2
            )

            bookmaker_probs.append(
                (p1, px, p2)
            )

            bookmakers_used += 1

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

    if not bookmaker_probs:
        return []

    consensus_home = (
        sum(x[0] for x in bookmaker_probs)
        / len(bookmaker_probs)
    )

    consensus_draw = (
        sum(x[1] for x in bookmaker_probs)
        / len(bookmaker_probs)
    )

    consensus_away = (
        sum(x[2] for x in bookmaker_probs)
        / len(bookmaker_probs)
    )

    fair = {
        home: 1 / consensus_home,
        "Draw": 1 / consensus_draw,
        away: 1 / consensus_away
    }

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

        fair_odd = fair[outcome]

        edge = (
            max_odd / fair_odd
        ) - 1

        signal = edge >= EDGE_MIN

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

            "fair_odd":
                round(
                    fair_odd,
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
                    edge * 100,
                    2
                ),

            "signal":
                "YES"
                if signal
                else "NO",

            "bookmakers_used":
                bookmakers_used
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
# GOOGLE SHEETS : ENREGISTREMENT
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
            r["fair_odd"],
            r["best_odd"],
            r["bookmaker"],
            r["edge_pct"],
            r["signal"],
            r["bookmakers_used"]
        ])

    sheet_matchs.append_rows(
        data,
        value_input_option="USER_ENTERED"
    )


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


def enregistrer_signals(rows):

    signals = [
        x
        for x in rows
        if x["signal"] == "YES"
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
            r["fair_odd"],
            r["best_odd"],
            r["bookmaker"],
            r["edge_pct"],

            "",     # result
            "",     # closing_odd
            "",     # clv_pct
            ""      # profit
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

st.title("📊 Value Scanner V5")

st.caption(
    "Consensus bookmaker → "
    "Fair odds → "
    "Meilleure cote → "
    "Edge ≥ 5%"
)


# ------------------------------------------------------------
# CRÉDITS ACTUELS
# ------------------------------------------------------------

st.subheader("💳 API")

st.write(
    "Le nombre exact de crédits sera mis à jour "
    "après chaque scan."
)


# ------------------------------------------------------------
# CHOIX LIGUES
# ------------------------------------------------------------

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

    credits_before = None
    credits_after = None
    credits_used_scan = 0

    first_api_used = None
    last_api_used = None

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

            try:

                api_used = int(
                    result["used"]
                )

                if first_api_used is None:
                    first_api_used = (
                        api_used - 1
                    )

                last_api_used = api_used

            except:
                pass

            try:

                credits_after = int(
                    result["remaining"]
                )

            except:
                pass

    if (
        first_api_used is not None
        and last_api_used is not None
    ):

        credits_used_scan = (
            last_api_used
            - first_api_used
        )

        if credits_after is not None:

            credits_before = (
                credits_after
                + credits_used_scan
            )

    # --------------------------------------------------------
    # SAUVEGARDE
    # --------------------------------------------------------

    enregistrer_matchs(
        all_rows
    )

    new_signals = enregistrer_signals(
        all_rows
    )

    signal_count = sum(

        1
        for r in all_rows
        if r["signal"] == "YES"
    )

    issues_count = len(all_rows)

    sheet_scans.append_row([

        scan_id,

        scan_time.isoformat(
            timespec="seconds"
        ),

        len(selected_leagues),

        total_matches,

        issues_count,

        signal_count,

        credits_before
        if credits_before is not None
        else "",

        credits_used_scan,

        credits_after
        if credits_after is not None
        else ""

    ])

    # --------------------------------------------------------
    # RÉSUMÉ
    # --------------------------------------------------------

    st.success(
        "Scan terminé"
    )

    c1, c2, c3, c4 = st.columns(4)

    c1.metric(
        "Matchs",
        total_matches
    )

    c2.metric(
        "Issues analysées",
        issues_count
    )

    c3.metric(
        "Values ≥5%",
        signal_count
    )

    c4.metric(
        "Nouveaux signals",
        new_signals
    )

    if credits_after is not None:

        st.metric(
            "Crédits API restants",
            credits_after
        )

    if credits_used_scan:

        st.write(
            f"**Crédits utilisés pour ce scan : "
            f"{credits_used_scan}**"
        )

        if credits_after:

            estimated_scans = (
                credits_after
                // credits_used_scan
            )

            st.write(
                f"Environ **{estimated_scans} "
                f"scans similaires** encore possibles."
            )

    # --------------------------------------------------------
    # TABLEAU COMPLET
    # --------------------------------------------------------

    st.subheader(
        "📋 Détail du scan"
    )

    df = pd.DataFrame(
        all_rows
    )

    if not df.empty:

        st.dataframe(
            df,
            use_container_width=True,
            hide_index=True
        )

    # --------------------------------------------------------
    # VALUES
    # --------------------------------------------------------

    signals_df = df[
        df["signal"] == "YES"
    ] if not df.empty else pd.DataFrame()

    st.subheader(
        "🚨 Values détectées"
    )

    if signals_df.empty:

        st.info(
            "Aucune value ≥5 %."
        )

    else:

        st.dataframe(
            signals_df,
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
        "Signals"
    )

    st.dataframe(
        signals_df,
        use_container_width=True,
        hide_index=True
    )


# ============================================================
# DOWNLOAD EXCEL
# ============================================================

st.divider()

st.header(
    "📥 Export"
)

excel_file = creer_excel()

st.download_button(

    label="Télécharger l'historique Excel",

    data=excel_file,

    file_name="value_scanner_historique.xlsx",

    mime=(
        "application/vnd.openxmlformats-"
        "officedocument.spreadsheetml.sheet"
    )
)
