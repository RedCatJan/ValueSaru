import streamlit as st
import requests
import gspread
import pandas as pd

from google.oauth2.service_account import Credentials
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
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

# Fenêtre officielle de Closing
CLOSING_MIN_MINUTES = 5
CLOSING_MAX_MINUTES = 60

PARIS_TZ = ZoneInfo("Europe/Paris")

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
    "fair_odd",
    "best_odd",
    "bookmaker",
    "edge_pct",
    "signal",
    "bookmakers_used",
    "fair_odd_historical",
    "edge_historical_pct",
    "signal_historical"
]


def verifier_headers_matchs():

    first_row = sheet_matchs.row_values(1)

    if first_row != MATCHS_HEADERS:

        sheet_matchs.update(
            "A1:R1",
            [MATCHS_HEADERS]
        )


verifier_headers_matchs()


# ============================================================
# OUTILS TEMPS
# ============================================================

def utc_to_paris(dt_utc):

    return dt_utc.astimezone(PARIS_TZ)


def format_paris(dt_utc):

    dt_paris = utc_to_paris(dt_utc)

    return dt_paris.strftime(
        "%d/%m/%Y %H:%M"
    )


def calcul_timing(minutes_before_match):

    if minutes_before_match <= 0:
        return "STARTED"

    if minutes_before_match < CLOSING_MIN_MINUTES:
        return "TOO_LATE"

    if minutes_before_match <= CLOSING_MAX_MINUTES:
        return "CLOSING"

    return "EARLY"


# ============================================================
# PLANNING GRATUIT
# Endpoint EVENTS
# ============================================================

@st.cache_data(ttl=300)
def recuperer_events_ligue(
    league_name,
    sport_key
):

    url = (
        "https://api.the-odds-api.com/"
        f"v4/sports/{sport_key}/events"
    )

    params = {
        "apiKey": API_KEY
    }

    try:

        response = requests.get(
            url,
            params=params,
            timeout=20
        )

    except Exception as e:

        return [], str(e)

    if response.status_code != 200:

        return [], response.text

    events = response.json()

    rows = []

    now_utc = datetime.now(timezone.utc)
    now_paris = utc_to_paris(now_utc)

    for event in events:

        commence_time = event.get(
            "commence_time"
        )

        if not commence_time:
            continue

        try:

            match_time_utc = (
                datetime.fromisoformat(
                    commence_time.replace(
                        "Z",
                        "+00:00"
                    )
                )
            )

        except:
            continue

        match_time_paris = utc_to_paris(
            match_time_utc
        )

        minutes_before = (
            match_time_utc - now_utc
        ).total_seconds() / 60

        timing = calcul_timing(
            minutes_before
        )

        closing_start = (
            match_time_paris
            - timedelta(
                minutes=CLOSING_MAX_MINUTES
            )
        )

        closing_end = (
            match_time_paris
            - timedelta(
                minutes=CLOSING_MIN_MINUTES
            )
        )

        rows.append({

            "league": league_name,

            "home":
                event.get(
                    "home_team",
                    ""
                ),

            "away":
                event.get(
                    "away_team",
                    ""
                ),

            "match_time_utc":
                match_time_utc,

            "match_time_paris":
                match_time_paris,

            "date_paris":
                match_time_paris.date(),

            "kickoff":
                match_time_paris.strftime(
                    "%H:%M"
                ),

            "closing_start_dt":
                closing_start,

            "closing_end_dt":
                closing_end,

            "closing_window":
                (
                    closing_start.strftime("%H:%M")
                    + " → "
                    + closing_end.strftime("%H:%M")
                ),

            "minutes_before_match":
                round(
                    minutes_before,
                    1
                ),

            "timing":
                timing
        })

    return rows, None


def recuperer_planning(
    leagues
):

    all_events = []
    errors = []

    for league_name in leagues:

        rows, error = (
            recuperer_events_ligue(
                league_name,
                SPORTS[league_name]
            )
        )

        all_events.extend(rows)

        if error:
            errors.append(
                f"{league_name}: {error}"
            )

    return all_events, errors


# ============================================================
# CALCUL V5
# ============================================================

def demarger(
    c1,
    cx,
    c2
):

    p1 = 1 / c1
    px = 1 / cx
    p2 = 1 / c2

    total = (
        p1
        + px
        + p2
    )

    return (
        p1 / total,
        px / total,
        p2 / total
    )


# ============================================================
# ANALYSE D'UN MATCH
# ============================================================

def analyser_match(
    match,
    league_name,
    scan_id
):

    home = match["home_team"]
    away = match["away_team"]

    commence_time = (
        match["commence_time"]
    )

    match_time = (
        datetime.fromisoformat(
            commence_time.replace(
                "Z",
                "+00:00"
            )
        )
    )

    now = datetime.now(
        timezone.utc
    )

    minutes_before_match = (
        match_time - now
    ).total_seconds() / 60

    if minutes_before_match <= 0:
        return []

    timing = calcul_timing(
        minutes_before_match
    )

    # ========================================================
    # V5
    # ========================================================

    bookmaker_probs = []

    # ========================================================
    # HISTORIQUE
    # moyenne brute des cotes
    # ========================================================

    raw_odds_home = []
    raw_odds_draw = []
    raw_odds_away = []

    # ========================================================
    # MEILLEURES COTES
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

    for bookmaker in match.get(
        "bookmakers",
        []
    ):

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

                odds[
                    outcome["name"]
                ] = outcome["price"]

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

            c1 = float(
                odds[home]
            )

            cx = float(
                odds[draw_key]
            )

            c2 = float(
                odds[away]
            )

            if (
                c1 <= 1
                or cx <= 1
                or c2 <= 1
            ):
                continue

            # ------------------------------------------------
            # Historique
            # ------------------------------------------------

            raw_odds_home.append(
                c1
            )

            raw_odds_draw.append(
                cx
            )

            raw_odds_away.append(
                c2
            )

            # ------------------------------------------------
            # V5
            # ------------------------------------------------

            p1, px, p2 = demarger(
                c1,
                cx,
                c2
            )

            bookmaker_probs.append(
                (
                    p1,
                    px,
                    p2
                )
            )

            bookmakers_used += 1

            # ------------------------------------------------
            # Best odds
            # ------------------------------------------------

            if (
                c1
                > best[home]["odd"]
            ):

                best[home] = {
                    "odd": c1,
                    "book": book_name
                }

            if (
                cx
                > best["Draw"]["odd"]
            ):

                best["Draw"] = {
                    "odd": cx,
                    "book": book_name
                }

            if (
                c2
                > best[away]["odd"]
            ):

                best[away] = {
                    "odd": c2,
                    "book": book_name
                }

    if not bookmaker_probs:
        return []

    if (
        not raw_odds_home
        or not raw_odds_draw
        or not raw_odds_away
    ):
        return []

    # ========================================================
    # FAIR V5
    # ========================================================

    consensus_home_v5 = (
        sum(
            x[0]
            for x in bookmaker_probs
        )
        / len(bookmaker_probs)
    )

    consensus_draw_v5 = (
        sum(
            x[1]
            for x in bookmaker_probs
        )
        / len(bookmaker_probs)
    )

    consensus_away_v5 = (
        sum(
            x[2]
            for x in bookmaker_probs
        )
        / len(bookmaker_probs)
    )

    fair_v5 = {

        home:
            1 / consensus_home_v5,

        "Draw":
            1 / consensus_draw_v5,

        away:
            1 / consensus_away_v5
    }

    # ========================================================
    # FAIR HISTORIQUE
    #
    # Avg Odds -> Implicite -> Démarge
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

    p_home_raw = (
        1 / avg_home
    )

    p_draw_raw = (
        1 / avg_draw
    )

    p_away_raw = (
        1 / avg_away
    )

    total_raw = (
        p_home_raw
        + p_draw_raw
        + p_away_raw
    )

    hist_prob_home = (
        p_home_raw
        / total_raw
    )

    hist_prob_draw = (
        p_draw_raw
        / total_raw
    )

    hist_prob_away = (
        p_away_raw
        / total_raw
    )

    fair_historical = {

        home:
            1 / hist_prob_home,

        "Draw":
            1 / hist_prob_draw,

        away:
            1 / hist_prob_away
    }

    # ========================================================
    # RESULTATS
    # ========================================================

    rows = []

    timestamp = datetime.now(
        timezone.utc
    ).isoformat(
        timespec="seconds"
    )

    for outcome in [
        home,
        "Draw",
        away
    ]:

        max_odd = (
            best[outcome]["odd"]
        )

        if max_odd == 0:
            continue

        fair_odd_v5 = (
            fair_v5[outcome]
        )

        fair_odd_hist = (
            fair_historical[
                outcome
            ]
        )

        edge_v5 = (
            max_odd
            / fair_odd_v5
        ) - 1

        edge_historical = (
            max_odd
            / fair_odd_hist
        ) - 1

        signal_v5 = (
            edge_v5
            >= EDGE_MIN
        )

        signal_historical = (
            edge_historical
            >= EDGE_MIN
        )

        rows.append({

            "scan_id":
                scan_id,

            "timestamp":
                timestamp,

            "league":
                league_name,

            "home":
                home,

            "away":
                away,

            "match_time":
                commence_time,

            "minutes_before_match":
                round(
                    minutes_before_match,
                    1
                ),

            "timing":
                timing,

            "outcome":
                outcome,

            # V5
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
                best[
                    outcome
                ]["book"],

            "edge_pct":
                round(
                    edge_v5
                    * 100,
                    2
                ),

            "signal":
                (
                    "YES"
                    if signal_v5
                    else "NO"
                ),

            "bookmakers_used":
                bookmakers_used,

            # HISTORIQUE
            "fair_odd_historical":
                round(
                    fair_odd_hist,
                    4
                ),

            "edge_historical_pct":
                round(
                    edge_historical
                    * 100,
                    2
                ),

            "signal_historical":
                (
                    "YES"
                    if signal_historical
                    else "NO"
                )
        })

    return rows


# ============================================================
# SCANNER UNE LIGUE AVEC LES COTES
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

        "apiKey":
            API_KEY,

        "regions":
            "fr",

        "markets":
            "h2h",

        "oddsFormat":
            "decimal"
    }

    try:

        response = requests.get(
            url,
            params=params,
            timeout=20
        )

    except Exception as e:

        return {

            "rows": [],

            "matches": 0,

            "remaining": None,

            "used": None,

            "last": None,

            "error": str(e)
        }

    remaining = (
        response.headers.get(
            "x-requests-remaining"
        )
    )

    used = (
        response.headers.get(
            "x-requests-used"
        )
    )

    last = (
        response.headers.get(
            "x-requests-last"
        )
    )

    if response.status_code != 200:

        return {

            "rows": [],

            "matches": 0,

            "remaining":
                remaining,

            "used":
                used,

            "last":
                last,

            "error":
                response.text
        }

    matches = (
        response.json()
    )

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

        "rows":
            rows,

        "matches":
            len(matches),

        "remaining":
            remaining,

        "used":
            used,

        "last":
            last,

        "error":
            None
    }


# ============================================================
# GOOGLE SHEETS : MATCHS
# ============================================================

def enregistrer_matchs(
    rows
):

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
            r[
                "minutes_before_match"
            ],
            r["timing"],
            r["outcome"],

            r["fair_odd"],
            r["best_odd"],
            r["bookmaker"],
            r["edge_pct"],
            r["signal"],
            r["bookmakers_used"],

            r[
                "fair_odd_historical"
            ],

            r[
                "edge_historical_pct"
            ],

            r[
                "signal_historical"
            ]
        ])

    sheet_matchs.append_rows(

        data,

        value_input_option=(
            "USER_ENTERED"
        )
    )


# ============================================================
# SIGNALS : ANTI DOUBLONS
# ============================================================

def recuperer_signal_keys():

    values = (
        sheet_signals
        .get_all_records()
    )

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

        keys.add(
            key
        )

    return keys


# ============================================================
# ENREGISTRER SIGNALS HISTORIQUES
# ============================================================

def enregistrer_signals(
    rows
):

    signals = [

        x

        for x in rows

        if (
            x["signal_historical"]
            == "YES"
        )
    ]

    if not signals:
        return 0

    existing_keys = (
        recuperer_signal_keys()
    )

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

        existing_keys.add(
            key
        )

        new_rows.append([

            r["scan_id"],

            r["timestamp"],

            r["league"],

            r["home"],

            r["away"],

            r["match_time"],

            r[
                "minutes_before_match"
            ],

            r["timing"],

            r["outcome"],

            r[
                "fair_odd_historical"
            ],

            r["best_odd"],

            r["bookmaker"],

            r[
                "edge_historical_pct"
            ],

            "",  # result

            "",  # closing_odd

            "",  # clv_pct

            ""   # profit
        ])

    if new_rows:

        sheet_signals.append_rows(

            new_rows,

            value_input_option=(
                "USER_ENTERED"
            )
        )

    return len(
        new_rows
    )


# ============================================================
# EXPORT EXCEL
# ============================================================

def creer_excel():

    scans = pd.DataFrame(
        sheet_scans
        .get_all_records()
    )

    matchs = pd.DataFrame(
        sheet_matchs
        .get_all_records()
    )

    signals = pd.DataFrame(
        sheet_signals
        .get_all_records()
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
# EXECUTION D'UN SCAN PAYANT
# ============================================================

def executer_scan(
    leagues_to_scan
):

    if not leagues_to_scan:

        st.warning(
            "Aucune ligue à scanner."
        )

        return

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

    with st.spinner(
        "Récupération des cotes..."
    ):

        for league_name in (
            leagues_to_scan
        ):

            result = scanner_ligue(
                league_name,
                SPORTS[
                    league_name
                ],
                scan_id
            )

            if result["error"]:

                st.warning(
                    f"{league_name} : "
                    "erreur API."
                )

                continue

            total_matches += (
                result["matches"]
            )

            all_rows.extend(
                result["rows"]
            )

            try:

                credits_used_scan += (
                    int(
                        result["last"]
                    )
                )

            except:
                pass

            try:

                credits_after = int(
                    result["remaining"]
                )

            except:
                pass

    credits_before = None

    if credits_after is not None:

        credits_before = (
            credits_after
            + credits_used_scan
        )

    # ========================================================
    # SAUVEGARDE
    # ========================================================

    enregistrer_matchs(
        all_rows
    )

    new_signals = (
        enregistrer_signals(
            all_rows
        )
    )

    signal_hist_count = sum(

        1

        for r in all_rows

        if (
            r[
                "signal_historical"
            ]
            == "YES"
        )
    )

    closing_rows = [

        r

        for r in all_rows

        if (
            r["timing"]
            == "CLOSING"
        )
    ]

    closing_signal_rows = [

        r

        for r in closing_rows

        if (
            r[
                "signal_historical"
            ]
            == "YES"
        )
    ]

    issues_count = len(
        all_rows
    )

    sheet_scans.append_row([

        scan_id,

        scan_time.isoformat(
            timespec="seconds"
        ),

        len(
            leagues_to_scan
        ),

        total_matches,

        issues_count,

        signal_hist_count,

        (
            credits_before
            if credits_before
            is not None
            else ""
        ),

        credits_used_scan,

        (
            credits_after
            if credits_after
            is not None
            else ""
        )
    ])

    # ========================================================
    # AFFICHAGE
    # ========================================================

    st.success(
        "Scan terminé"
    )

    c1, c2, c3, c4 = (
        st.columns(4)
    )

    c1.metric(
        "Matchs API",
        total_matches
    )

    c2.metric(
        "Matchs CLOSING",
        len(
            set(
                (
                    r["league"],
                    r["home"],
                    r["away"],
                    r["match_time"]
                )
                for r in closing_rows
            )
        )
    )

    c3.metric(
        "Signals historiques",
        signal_hist_count
    )

    c4.metric(
        "Signals CLOSING",
        len(
            closing_signal_rows
        )
    )

    # ========================================================
    # CREDITS
    # ========================================================

    st.subheader(
        "💳 Crédits"
    )

    c1, c2, c3 = (
        st.columns(3)
    )

    c1.metric(
        "Avant",
        (
            credits_before
            if credits_before
            is not None
            else "?"
        )
    )

    c2.metric(
        "Utilisés",
        credits_used_scan
    )

    c3.metric(
        "Restants",
        (
            credits_after
            if credits_after
            is not None
            else "?"
        )
    )

    if (
        credits_after
        is not None
        and credits_used_scan > 0
    ):

        estimated = (
            credits_after
            // credits_used_scan
        )

        st.caption(
            f"≈ {estimated} scans "
            f"similaires restants"
        )

    # ========================================================
    # TABLEAU CLOSING
    # ========================================================

    st.subheader(
        "🎯 Matchs actuellement CLOSING"
    )

    if not closing_rows:

        st.info(
            "Aucun match de ce scan "
            "n'est actuellement entre "
            "5 et 60 minutes du coup d'envoi."
        )

    else:

        closing_df = pd.DataFrame(
            closing_rows
        )

        display_cols = [

            "league",
            "home",
            "away",
            "minutes_before_match",
            "outcome",

            "fair_odd_historical",

            "best_odd",

            "bookmaker",

            "edge_historical_pct",

            "signal_historical"
        ]

        st.dataframe(
            closing_df[
                display_cols
            ],
            use_container_width=True,
            hide_index=True
        )

    # ========================================================
    # SIGNALS CLOSING
    # ========================================================

    st.subheader(
        "🚨 Values CLOSING ≥5 %"
    )

    if not closing_signal_rows:

        st.info(
            "Aucune value CLOSING ≥5 %."
        )

    else:

        signals_df = pd.DataFrame(
            closing_signal_rows
        )

        st.dataframe(
            signals_df,
            use_container_width=True,
            hide_index=True
        )

    # ========================================================
    # DETAIL COMPLET
    # ========================================================

    with st.expander(
        "📋 Voir tout le détail du scan"
    ):

        df = pd.DataFrame(
            all_rows
        )

        if not df.empty:

            st.dataframe(
                df,
                use_container_width=True,
                hide_index=True
            )

    st.caption(
        f"Nouveaux signaux ajoutés "
        f"au journal : {new_signals}"
    )


# ============================================================
# TITRE
# ============================================================

st.title(
    "📊 Value Scanner V7"
)

st.caption(
    "Planning gratuit + "
    "scanner Closing + "
    "formule historique"
)

st.info(
    "🎯 Validation officielle : "
    "CLOSING (5–60 min avant match) "
    "+ edge historique ≥5 %."
)


# ============================================================
# ONGLETS
# ============================================================

tab_planning, tab_scan, tab_history = (
    st.tabs([
        "📅 Planning Closing",
        "🎯 Scanner",
        "📊 Historique"
    ])
)


# ============================================================
# TAB 1 : PLANNING
# ============================================================

with tab_planning:

    st.header(
        "📅 Matchs du jour"
    )

    st.write(
        "Cette page récupère les horaires "
        "des matchs afin de te dire "
        "quelles ligues scanner et quand."
    )

    planning_leagues = st.multiselect(

        "Ligues à surveiller",

        list(
            SPORTS.keys()
        ),

        default=list(
            SPORTS.keys()
        ),

        key="planning_leagues"
    )

    if st.button(
        "📅 Actualiser le planning",
        use_container_width=True
    ):

        recuperer_events_ligue.clear()

    planning_rows, errors = (
        recuperer_planning(
            planning_leagues
        )
    )

    now_paris = datetime.now(
        PARIS_TZ
    )

    today_paris = (
        now_paris.date()
    )

    # ========================================================
    # MATCHS DU JOUR UNIQUEMENT
    # ========================================================

    today_rows = [

        r

        for r in planning_rows

        if (
            r["date_paris"]
            == today_paris
        )
        and (
            r["minutes_before_match"]
            > 0
        )
    ]

    today_rows = sorted(
        today_rows,
        key=lambda x:
            x["match_time_paris"]
    )

    st.metric(
        "Matchs suivis aujourd'hui",
        len(today_rows)
    )

    if not today_rows:

        st.info(
            "Aucun match à venir aujourd'hui "
            "dans les ligues sélectionnées."
        )

    else:

        # ====================================================
        # MATCHS EN CLOSING MAINTENANT
        # ====================================================

        closing_now = [

            r

            for r in today_rows

            if (
                r["timing"]
                == "CLOSING"
            )
        ]

        leagues_closing_now = sorted(
            set(
                r["league"]
                for r in closing_now
            )
        )

        if closing_now:

            st.success(
                f"🎯 {len(closing_now)} match(s) "
                f"sont actuellement en CLOSING."
            )

            st.write(
                "**Ligues à scanner maintenant :** "
                + ", ".join(
                    leagues_closing_now
                )
            )

            st.write(
                f"**Coût estimé : "
                f"{len(leagues_closing_now)} crédit(s)**"
            )

            if st.button(
                "🚨 Scanner les closings maintenant",
                type="primary",
                use_container_width=True
            ):

                executer_scan(
                    leagues_closing_now
                )

        else:

            st.info(
                "Aucun match n'est actuellement "
                "dans la fenêtre CLOSING."
            )

        # ====================================================
        # PROCHAIN SCAN RECOMMANDÉ
        # ====================================================

        future_windows = [

            r

            for r in today_rows

            if (
                r["closing_start_dt"]
                > now_paris
            )
        ]

        if future_windows:

            next_start = min(
                r["closing_start_dt"]
                for r in future_windows
            )

            # Tous les matchs dont le closing
            # commence dans une fenêtre de 10 min
            next_group = [

                r

                for r in future_windows

                if abs(
                    (
                        r["closing_start_dt"]
                        - next_start
                    ).total_seconds()
                ) <= 600
            ]

            next_leagues = sorted(
                set(
                    r["league"]
                    for r in next_group
                )
            )

            minutes_until = (
                next_start
                - now_paris
            ).total_seconds() / 60

            st.subheader(
                "⏰ Prochain scan recommandé"
            )

            st.write(
                f"**À partir de : "
                f"{next_start.strftime('%H:%M')}**"
            )

            st.write(
                f"Dans environ "
                f"**{max(0, round(minutes_until))} min**"
            )

            st.write(
                "**Ligues :** "
                + ", ".join(
                    next_leagues
                )
            )

            st.write(
                f"**Coût estimé : "
                f"{len(next_leagues)} crédit(s)**"
            )

        # ====================================================
        # TABLEAU DU JOUR
        # ====================================================

        st.subheader(
            "🗓️ Planning complet"
        )

        planning_display = []

        for r in today_rows:

            if r["timing"] == "CLOSING":

                action = (
                    "🎯 SCANNER MAINTENANT"
                )

            elif r["timing"] == "TOO_LATE":

                action = (
                    "⚠️ Trop proche"
                )

            else:

                action = (
                    "⏳ Attendre"
                )

            planning_display.append({

                "Heure":
                    r["kickoff"],

                "Ligue":
                    r["league"],

                "Domicile":
                    r["home"],

                "Extérieur":
                    r["away"],

                "Fenêtre Closing":
                    r[
                        "closing_window"
                    ],

                "Minutes restantes":
                    round(
                        r[
                            "minutes_before_match"
                        ]
                    ),

                "Statut":
                    r["timing"],

                "Action":
                    action
            })

        planning_df = pd.DataFrame(
            planning_display
        )

        st.dataframe(
            planning_df,
            use_container_width=True,
            hide_index=True
        )

    if errors:

        with st.expander(
            "⚠️ Erreurs planning"
        ):

            for error in errors:
                st.write(error)


# ============================================================
# TAB 2 : SCANNER MANUEL
# ============================================================

with tab_scan:

    st.header(
        "🎯 Scanner les cotes"
    )

    st.write(
        "Utilise ce bouton si tu veux "
        "scanner manuellement certaines ligues."
    )

    selected_leagues = (
        st.multiselect(

            "Ligues à scanner",

            list(
                SPORTS.keys()
            ),

            default=[
                "Japon J-League"
            ],

            key="manual_scan"
        )
    )

    st.caption(
        f"Coût estimé : "
        f"{len(selected_leagues)} crédit(s)"
    )

    if st.button(
        "🔍 Scanner les ligues sélectionnées",
        use_container_width=True
    ):

        executer_scan(
            selected_leagues
        )


# ============================================================
# TAB 3 : HISTORIQUE
# ============================================================

with tab_history:

    st.header(
        "📊 Historique"
    )

    if st.button(
        "Actualiser l'historique"
    ):

        scans_df = pd.DataFrame(
            sheet_scans
            .get_all_records()
        )

        signals_df = pd.DataFrame(
            sheet_signals
            .get_all_records()
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

    # ========================================================
    # EXPORT
    # ========================================================

    st.subheader(
        "📥 Export Excel"
    )

    try:

        excel_file = (
            creer_excel()
        )

        st.download_button(

            label=(
                "Télécharger l'historique Excel"
            ),

            data=excel_file,

            file_name=(
                "value_scanner_historique.xlsx"
            ),

            mime=(
                "application/"
                "vnd.openxmlformats-officedocument."
                "spreadsheetml.sheet"
            )
        )

    except:

        st.warning(
            "Export Excel temporairement "
            "indisponible."
        )
