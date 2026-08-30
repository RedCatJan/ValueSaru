import streamlit as st
import requests
import gspread
import pandas as pd

from google.oauth2.service_account import Credentials
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from io import BytesIO


# ============================================================
# CONFIGURATION
# ============================================================

st.set_page_config(
    page_title="Value Scanner",
    page_icon="📊",
    layout="wide",
)

EDGE_MIN = 0.05
CLOSING_MIN_MINUTES = 5
CLOSING_MAX_MINUTES = 60

MIN_BOOKMAKERS = 3
OUTLIER_RATIO_MAX = 1.20

MONTHLY_CREDIT_BUDGET = 500
CREDIT_RESERVE = 100

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
        "https://www.googleapis.com/auth/drive",
    ]

    credentials = Credentials.from_service_account_info(
        dict(st.secrets["gcp_service_account"]),
        scopes=scopes,
    )

    client = gspread.authorize(credentials)

    return client.open_by_key(GOOGLE_SHEET_ID)


spreadsheet = connexion_google()


def get_or_create_worksheet(title, rows=3000, cols=40):

    try:

        return spreadsheet.worksheet(title)

    except gspread.WorksheetNotFound:

        return spreadsheet.add_worksheet(
            title=title,
            rows=rows,
            cols=cols,
        )


sheet_scans = get_or_create_worksheet("Scans")
sheet_matchs = get_or_create_worksheet("Matchs")
sheet_signals = get_or_create_worksheet("Signals")


SCANS_HEADERS = [
    "scan_id",
    "timestamp_paris",
    "scan_type",
    "ligues_scannées",
    "matchs_api",
    "matchs_closing",
    "issues_scannées",
    "signals_officiels",
    "credits_before",
    "credits_used",
    "credits_remaining",
    "status",
    "error_message",
]


MATCHS_HEADERS = [
    "scan_id",
    "timestamp_paris",
    "league",
    "event_id",
    "home",
    "away",
    "match_time_paris",
    "minutes_before_match",
    "timing",
    "outcome",
    "fair_odd_v5",
    "best_odd",
    "bookmaker",
    "edge_v5_pct",
    "signal_v5",
    "bookmakers_used",
    "fair_odd_historical",
    "edge_historical_pct",
    "signal_historical",
    "quality_status",
    "quality_note",
    "official_signal",
]


SIGNALS_HEADERS = [
    "signal_id",
    "event_id",
    "first_scan_id",
    "detected_at_paris",
    "league",
    "home",
    "away",
    "match_time_paris",
    "minutes_before_match",
    "outcome",
    "fair_odd_historical",
    "bet_odd",
    "bookmaker",
    "edge_historical_pct",
    "quality_status",
    "status",
    "home_score",
    "away_score",
    "result",
    "profit",
    "closing_odd",
    "clv_pct",
    "last_snapshot_at_paris",
]


def ensure_headers(ws, required_headers):

    current = ws.row_values(1)

    if not current:

        ws.update(
            "A1",
            [required_headers]
        )

        return required_headers

    missing = [
        h
        for h in required_headers
        if h not in current
    ]

    if missing:

        new_headers = (
            current
            + missing
        )

        ws.update(
            "A1",
            [new_headers]
        )

        return new_headers

    return current


ensure_headers(
    sheet_scans,
    SCANS_HEADERS
)

ensure_headers(
    sheet_matchs,
    MATCHS_HEADERS
)

ensure_headers(
    sheet_signals,
    SIGNALS_HEADERS
)


def append_dict_rows(
    ws,
    rows,
    required_headers
):

    if not rows:
        return

    headers = ensure_headers(
        ws,
        required_headers
    )

    values = [

        [
            row.get(
                header,
                ""
            )
            for header in headers
        ]

        for row in rows
    ]

    ws.append_rows(
        values,
        value_input_option="USER_ENTERED",
    )


def update_sheet_row_by_key(
    ws,
    key_col,
    key_value,
    updates
):

    values = ws.get_all_values()

    if not values:
        return False

    headers = values[0]

    missing = [
        col
        for col in updates
        if col not in headers
    ]

    if missing:

        headers = ensure_headers(
            ws,
            headers + missing
        )

    if key_col not in headers:
        return False

    key_idx = headers.index(
        key_col
    )

    for row_num, row in enumerate(
        values[1:],
        start=2
    ):

        current = (
            row[key_idx]
            if key_idx < len(row)
            else ""
        )

        if str(current) == str(key_value):

            for col_name, new_value in updates.items():

                col_idx = (
                    headers.index(
                        col_name
                    )
                    + 1
                )

                ws.update_cell(
                    row_num,
                    col_idx,
                    new_value
                )

            return True

    return False


# ============================================================
# TEMPS
# ============================================================

def now_utc():

    return datetime.now(
        timezone.utc
    )


def now_paris():

    return now_utc().astimezone(
        PARIS_TZ
    )


def parse_utc(value):

    return datetime.fromisoformat(
        value.replace(
            "Z",
            "+00:00"
        )
    )


def format_paris(dt):

    return dt.astimezone(
        PARIS_TZ
    ).strftime(
        "%d/%m/%Y %H:%M:%S"
    )


def format_match_paris(dt):

    return dt.astimezone(
        PARIS_TZ
    ).strftime(
        "%d/%m/%Y %H:%M"
    )


def calcul_timing(
    minutes_before_match
):

    if minutes_before_match <= 0:

        return "STARTED"

    if (
        minutes_before_match
        < CLOSING_MIN_MINUTES
    ):

        return "TOO_LATE"

    if (
        minutes_before_match
        <= CLOSING_MAX_MINUTES
    ):

        return "CLOSING"

    return "EARLY"


# ============================================================
# PLANNING
# ============================================================

@st.cache_data(ttl=300)
def fetch_events_for_league(
    league_name,
    sport_key
):

    url = (
        "https://api.the-odds-api.com/"
        f"v4/sports/{sport_key}/events"
    )

    response = requests.get(
        url,
        params={
            "apiKey": API_KEY
        },
        timeout=20,
    )

    if response.status_code != 200:

        return {
            "league": league_name,
            "events": [],
            "error": response.text,
        }

    return {
        "league": league_name,
        "events": response.json(),
        "error": None,
    }


def build_planning(
    selected_leagues
):

    rows = []
    errors = []

    current_utc = now_utc()

    for league_name in selected_leagues:

        result = (
            fetch_events_for_league(
                league_name,
                SPORTS[
                    league_name
                ],
            )
        )

        if result["error"]:

            errors.append(
                f"{league_name}: "
                f"{result['error']}"
            )

            continue

        for event in result["events"]:

            commence_time = (
                event.get(
                    "commence_time"
                )
            )

            if not commence_time:
                continue

            try:

                match_utc = (
                    parse_utc(
                        commence_time
                    )
                )

            except Exception:

                continue

            minutes_before = (
                match_utc
                - current_utc
            ).total_seconds() / 60

            if minutes_before <= 0:
                continue

            match_paris = (
                match_utc.astimezone(
                    PARIS_TZ
                )
            )

            closing_start = (
                match_paris
                - pd.Timedelta(
                    minutes=(
                        CLOSING_MAX_MINUTES
                    )
                )
            )

            closing_end = (
                match_paris
                - pd.Timedelta(
                    minutes=(
                        CLOSING_MIN_MINUTES
                    )
                )
            )

            rows.append({

                "league":
                    league_name,

                "event_id":
                    event.get(
                        "id",
                        ""
                    ),

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

                "match_dt_paris":
                    match_paris,

                "kickoff":
                    match_paris.strftime(
                        "%H:%M"
                    ),

                "minutes_before_match":
                    round(
                        minutes_before,
                        1
                    ),

                "timing":
                    calcul_timing(
                        minutes_before
                    ),

                "closing_start":
                    closing_start.to_pydatetime(),

                "closing_window":
                    (
                        closing_start.strftime(
                            "%H:%M"
                        )
                        + " → "
                        + closing_end.strftime(
                            "%H:%M"
                        )
                    ),
            })

    rows = sorted(
        rows,
        key=lambda x:
            x["match_dt_paris"],
    )

    return rows, errors


# ============================================================
# VALUE CALCULATIONS
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
        p2 / total,
    )


def quality_check(values):

    clean = sorted(

        [
            float(x)
            for x in values
            if float(x) > 1
        ],

        reverse=True,
    )

    if len(clean) < MIN_BOOKMAKERS:

        return (
            "VERIFY",
            (
                f"Seulement "
                f"{len(clean)} "
                f"bookmaker(s)"
            ),
        )

    if (
        len(clean) >= 2
        and clean[1] > 0
        and (
            clean[0]
            / clean[1]
            > OUTLIER_RATIO_MAX
        )
    ):

        return (
            "VERIFY",
            (
                f"Cote "
                f"{clean[0]:.2f} "
                f"très éloignée "
                f"de la 2e "
                f"{clean[1]:.2f}"
            ),
        )

    return (
        "PASS",
        ""
    )


def analyser_match(
    match,
    league_name,
    scan_id
):

    home = (
        match[
            "home_team"
        ]
    )

    away = (
        match[
            "away_team"
        ]
    )

    event_id = (
        match.get(
            "id",
            ""
        )
    )

    commence_time = (
        match[
            "commence_time"
        ]
    )

    match_time = (
        parse_utc(
            commence_time
        )
    )

    minutes_before = (
        match_time
        - now_utc()
    ).total_seconds() / 60

    timing = calcul_timing(
        minutes_before
    )

    # OFFICIEL = CLOSING UNIQUEMENT
    if timing != "CLOSING":

        return []

    bookmaker_probs = []

    raw_home = []
    raw_draw = []
    raw_away = []

    all_odds = {

        home:
            [],

        "Draw":
            [],

        away:
            [],
    }

    best = {

        home: {
            "odd": 0,
            "book": None,
        },

        "Draw": {
            "odd": 0,
            "book": None,
        },

        away: {
            "odd": 0,
            "book": None,
        },
    }

    bookmakers_used = 0

    for bookmaker in match.get(
        "bookmakers",
        [],
    ):

        book_name = (
            bookmaker.get(
                "title",
                "Inconnu",
            )
        )

        for market in bookmaker.get(
            "markets",
            [],
        ):

            if (
                market.get("key")
                != "h2h"
            ):

                continue

            odds = {

                outcome["name"]:
                    float(
                        outcome[
                            "price"
                        ]
                    )

                for outcome in market.get(
                    "outcomes",
                    [],
                )

                if (
                    "name"
                    in outcome
                    and
                    "price"
                    in outcome
                )
            }

            if (
                home not in odds
                or
                away not in odds
            ):

                continue

            draw_key = next(

                (
                    name
                    for name in odds
                    if (
                        name.lower()
                        == "draw"
                    )
                ),

                None,
            )

            if draw_key is None:

                continue

            c1 = odds[home]
            cx = odds[draw_key]
            c2 = odds[away]

            if (
                c1 <= 1
                or cx <= 1
                or c2 <= 1
            ):

                continue

            raw_home.append(
                c1
            )

            raw_draw.append(
                cx
            )

            raw_away.append(
                c2
            )

            all_odds[
                home
            ].append(
                c1
            )

            all_odds[
                "Draw"
            ].append(
                cx
            )

            all_odds[
                away
            ].append(
                c2
            )

            bookmaker_probs.append(
                demarger(
                    c1,
                    cx,
                    c2,
                )
            )

            bookmakers_used += 1

            if (
                c1
                > best[
                    home
                ]["odd"]
            ):

                best[home] = {
                    "odd": c1,
                    "book": book_name,
                }

            if (
                cx
                > best[
                    "Draw"
                ]["odd"]
            ):

                best["Draw"] = {
                    "odd": cx,
                    "book": book_name,
                }

            if (
                c2
                > best[
                    away
                ]["odd"]
            ):

                best[away] = {
                    "odd": c2,
                    "book": book_name,
                }

    if not bookmaker_probs:

        return []

    # ========================================================
    # V5
    # ========================================================

    p_home_v5 = (

        sum(
            x[0]
            for x in bookmaker_probs
        )

        / len(
            bookmaker_probs
        )
    )

    p_draw_v5 = (

        sum(
            x[1]
            for x in bookmaker_probs
        )

        / len(
            bookmaker_probs
        )
    )

    p_away_v5 = (

        sum(
            x[2]
            for x in bookmaker_probs
        )

        / len(
            bookmaker_probs
        )
    )

    fair_v5 = {

        home:
            1 / p_home_v5,

        "Draw":
            1 / p_draw_v5,

        away:
            1 / p_away_v5,
    }


    # ========================================================
    # HISTORIQUE
    # ========================================================

    avg_home = (
        sum(raw_home)
        / len(raw_home)
    )

    avg_draw = (
        sum(raw_draw)
        / len(raw_draw)
    )

    avg_away = (
        sum(raw_away)
        / len(raw_away)
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

    fair_historical = {

        home:
            1
            / (
                p_home_raw
                / total_raw
            ),

        "Draw":
            1
            / (
                p_draw_raw
                / total_raw
            ),

        away:
            1
            / (
                p_away_raw
                / total_raw
            ),
    }


    rows = []

    timestamp_paris = (
        format_paris(
            now_utc()
        )
    )

    for outcome in [
        home,
        "Draw",
        away,
    ]:

        max_odd = (
            best[
                outcome
            ]["odd"]
        )

        if max_odd == 0:

            continue

        fair_odd_v5 = (
            fair_v5[
                outcome
            ]
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

        edge_hist = (
            max_odd
            / fair_odd_hist
        ) - 1

        (
            quality_status,
            quality_note
        ) = quality_check(
            all_odds[
                outcome
            ]
        )

        hist_signal = (
            edge_hist
            >= EDGE_MIN
        )

        official = (
            hist_signal
            and
            quality_status
            == "PASS"
        )

        rows.append({

            "scan_id":
                scan_id,

            "timestamp_paris":
                timestamp_paris,

            "league":
                league_name,

            "event_id":
                event_id,

            "home":
                home,

            "away":
                away,

            "match_time_paris":
                format_match_paris(
                    match_time
                ),

            "minutes_before_match":
                round(
                    minutes_before,
                    1
                ),

            "timing":
                timing,

            "outcome":
                outcome,

            "fair_odd_v5":
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

            "edge_v5_pct":
                round(
                    edge_v5
                    * 100,
                    2
                ),

            "signal_v5":
                (
                    "YES"
                    if (
                        edge_v5
                        >= EDGE_MIN
                    )
                    else "NO"
                ),

            "bookmakers_used":
                bookmakers_used,

            "fair_odd_historical":
                round(
                    fair_odd_hist,
                    4
                ),

            "edge_historical_pct":
                round(
                    edge_hist
                    * 100,
                    2
                ),

            "signal_historical":
                (
                    "YES"
                    if hist_signal
                    else "NO"
                ),

            "quality_status":
                quality_status,

            "quality_note":
                quality_note,

            "official_signal":
                (
                    "YES"
                    if official
                    else "NO"
                ),
        })

    return rows


# ============================================================
# SCAN ODDS
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
            "decimal",
    }

    response = requests.get(
        url,
        params=params,
        timeout=20,
    )

    result = {

        "rows":
            [],

        "matches_api":
            0,

        "remaining":
            response.headers.get(
                "x-requests-remaining"
            ),

        "last":
            response.headers.get(
                "x-requests-last"
            ),

        "error":
            None,
    }

    if response.status_code != 200:

        result["error"] = (
            response.text
        )

        return result

    matches = response.json()

    result["matches_api"] = (
        len(matches)
    )

    for match in matches:

        result["rows"].extend(

            analyser_match(
                match,
                league_name,
                scan_id,
            )
        )

    return result


# ============================================================
# SIGNALS
# ============================================================

def load_signals_records():

    return (
        sheet_signals
        .get_all_records()
    )


def signal_key(
    event_id,
    outcome
):

    return (
        f"{event_id}|"
        f"{outcome}"
    )


def make_signal_id(
    event_id,
    outcome
):

    safe_outcome = (

        str(outcome)
        .replace(
            " ",
            ""
        )
        .replace(
            "/",
            ""
        )
    )

    return (

        f"SIG-"
        f"{str(event_id)[:8]}-"
        f"{safe_outcome[:12]}"
    )


def upsert_signals(
    closing_rows
):

    existing = (
        load_signals_records()
    )

    existing_by_key = {

        signal_key(
            str(
                row.get(
                    "event_id",
                    ""
                )
            ),
            str(
                row.get(
                    "outcome",
                    ""
                )
            ),
        ):
            row

        for row in existing

        if (
            row.get(
                "event_id"
            )
            and
            row.get(
                "outcome"
            )
        )
    }

    created = 0
    updated = 0

    for row in closing_rows:

        key = signal_key(
            row[
                "event_id"
            ],
            row[
                "outcome"
            ],
        )

        # ----------------------------------------------------
        # Signal déjà existant
        # -> mise à jour CLV
        # ----------------------------------------------------

        if key in existing_by_key:

            signal = (
                existing_by_key[
                    key
                ]
            )

            bet_odd = float(
                signal.get(
                    "bet_odd"
                )
                or 0
            )

            closing_odd = float(
                row[
                    "best_odd"
                ]
            )

            clv_pct = (

                round(
                    (
                        bet_odd
                        / closing_odd
                        - 1
                    )
                    * 100,
                    2,
                )

                if (
                    bet_odd > 0
                    and
                    closing_odd > 0
                )

                else ""
            )

            update_sheet_row_by_key(
                sheet_signals,
                "signal_id",
                signal[
                    "signal_id"
                ],
                {
                    "closing_odd":
                        closing_odd,

                    "clv_pct":
                        clv_pct,

                    "last_snapshot_at_paris":
                        row[
                            "timestamp_paris"
                        ],
                },
            )

            updated += 1

            continue


        # ----------------------------------------------------
        # Pas de signal officiel
        # ----------------------------------------------------

        if (
            row[
                "official_signal"
            ]
            != "YES"
        ):

            continue


        # ----------------------------------------------------
        # Nouveau signal
        # ----------------------------------------------------

        signal_id = (
            make_signal_id(
                row[
                    "event_id"
                ],
                row[
                    "outcome"
                ],
            )
        )

        new_signal = {

            "signal_id":
                signal_id,

            "event_id":
                row[
                    "event_id"
                ],

            "first_scan_id":
                row[
                    "scan_id"
                ],

            "detected_at_paris":
                row[
                    "timestamp_paris"
                ],

            "league":
                row[
                    "league"
                ],

            "home":
                row[
                    "home"
                ],

            "away":
                row[
                    "away"
                ],

            "match_time_paris":
                row[
                    "match_time_paris"
                ],

            "minutes_before_match":
                row[
                    "minutes_before_match"
                ],

            "outcome":
                row[
                    "outcome"
                ],

            "fair_odd_historical":
                row[
                    "fair_odd_historical"
                ],

            "bet_odd":
                row[
                    "best_odd"
                ],

            "bookmaker":
                row[
                    "bookmaker"
                ],

            "edge_historical_pct":
                row[
                    "edge_historical_pct"
                ],

            "quality_status":
                row[
                    "quality_status"
                ],

            "status":
                "OPEN",

            "home_score":
                "",

            "away_score":
                "",

            "result":
                "",

            "profit":
                "",

            "closing_odd":
                row[
                    "best_odd"
                ],

            "clv_pct":
                0,

            "last_snapshot_at_paris":
                row[
                    "timestamp_paris"
                ],
        }

        append_dict_rows(
            sheet_signals,
            [
                new_signal
            ],
            SIGNALS_HEADERS,
        )

        existing_by_key[
            key
        ] = new_signal

        created += 1

    return (
        created,
        updated
    )


# ============================================================
# EXECUTION SCAN
# ============================================================

def executer_scan(
    leagues_to_scan
):

    if not leagues_to_scan:

        st.warning(
            "Aucune ligue à scanner."
        )

        return


    scan_time = (
        now_utc()
    )

    scan_id = (

        "SCAN-"

        + scan_time.strftime(
            "%Y%m%d-%H%M%S"
        )
    )


    started_row = {

        "scan_id":
            scan_id,

        "timestamp_paris":
            format_paris(
                scan_time
            ),

        "scan_type":
            "CLOSING",

        "ligues_scannées":
            len(
                leagues_to_scan
            ),

        "matchs_api":
            0,

        "matchs_closing":
            0,

        "issues_scannées":
            0,

        "signals_officiels":
            0,

        "credits_before":
            "",

        "credits_used":
            0,

        "credits_remaining":
            "",

        "status":
            "STARTED",

        "error_message":
            "",
    }


    append_dict_rows(
        sheet_scans,
        [
            started_row
        ],
        SCANS_HEADERS,
    )


    all_rows = []
    errors = []

    matches_api = 0
    credits_used = 0
    credits_remaining = None


    try:

        with st.spinner(
            "Scan des cotes CLOSING..."
        ):

            for league_name in (
                leagues_to_scan
            ):

                try:

                    result = (
                        scanner_ligue(
                            league_name,
                            SPORTS[
                                league_name
                            ],
                            scan_id,
                        )
                    )

                except Exception as exc:

                    errors.append(
                        f"{league_name}: "
                        f"{exc}"
                    )

                    continue


                matches_api += (
                    result[
                        "matches_api"
                    ]
                )

                all_rows.extend(
                    result[
                        "rows"
                    ]
                )

                try:

                    credits_used += int(
                        result[
                            "last"
                        ]
                        or 0
                    )

                except Exception:

                    pass


                try:

                    credits_remaining = int(
                        result[
                            "remaining"
                        ]
                    )

                except Exception:

                    pass


                if result["error"]:

                    errors.append(
                        f"{league_name}: "
                        f"{result['error']}"
                    )


        unique_closing_matches = {

            (
                row[
                    "league"
                ],

                row[
                    "event_id"
                ],
            )

            for row in all_rows
        }


        official_rows = [

            row

            for row in all_rows

            if (
                row[
                    "official_signal"
                ]
                == "YES"
            )
        ]


        credits_before = (

            credits_remaining
            + credits_used

            if (
                credits_remaining
                is not None
            )

            else ""
        )


        append_dict_rows(
            sheet_matchs,
            all_rows,
            MATCHS_HEADERS,
        )


        (
            created,
            updated
        ) = upsert_signals(
            all_rows
        )


        status = (

            "COMPLETE"

            if not errors

            else "PARTIAL"
        )


        update_sheet_row_by_key(
            sheet_scans,
            "scan_id",
            scan_id,
            {

                "matchs_api":
                    matches_api,

                "matchs_closing":
                    len(
                        unique_closing_matches
                    ),

                "issues_scannées":
                    len(
                        all_rows
                    ),

                "signals_officiels":
                    len(
                        official_rows
                    ),

                "credits_before":
                    credits_before,

                "credits_used":
                    credits_used,

                "credits_remaining":
                    (
                        credits_remaining

                        if (
                            credits_remaining
                            is not None
                        )

                        else ""
                    ),

                "status":
                    status,

                "error_message":
                    " | ".join(
                        errors
                    ),
            },
        )


        st.success(
            f"Scan enregistré : "
            f"{scan_id}"
        )


        c1, c2, c3, c4 = (
            st.columns(4)
        )


        c1.metric(
            "Matchs API",
            matches_api,
        )


        c2.metric(
            "Matchs CLOSING",
            len(
                unique_closing_matches
            ),
        )


        c3.metric(
            "Signals officiels",
            len(
                official_rows
            ),
        )


        c4.metric(
            "Nouveaux paper bets",
            created,
        )


        c1, c2, c3 = (
            st.columns(3)
        )


        c1.metric(
            "Crédits avant",
            (
                credits_before
                if credits_before != ""
                else "?"
            ),
        )


        c2.metric(
            "Crédits utilisés",
            credits_used,
        )


        c3.metric(
            "Crédits restants",
            (
                credits_remaining

                if (
                    credits_remaining
                    is not None
                )

                else "?"
            ),
        )


        if all_rows:

            df = pd.DataFrame(
                all_rows
            )


            st.subheader(
                "🎯 Détail CLOSING"
            )


            st.dataframe(
                df[
                    [
                        "league",
                        "home",
                        "away",
                        "minutes_before_match",
                        "outcome",
                        "fair_odd_historical",
                        "best_odd",
                        "bookmaker",
                        "edge_historical_pct",
                        "quality_status",
                        "quality_note",
                        "official_signal",
                    ]
                ],
                use_container_width=True,
                hide_index=True,
            )


            official_df = df[
                df[
                    "official_signal"
                ]
                == "YES"
            ]


            st.subheader(
                "🚨 Values officielles"
            )


            if official_df.empty:

                st.info(
                    "Aucune value officielle ≥5 %."
                )

            else:

                st.dataframe(
                    official_df,
                    use_container_width=True,
                    hide_index=True,
                )


        else:

            st.info(
                (
                    "Aucun match dans la "
                    "fenêtre CLOSING "
                    "5–60 min."
                )
            )


        if updated:

            st.caption(
                (
                    f"{updated} signal(aux) "
                    "existant(s) ont reçu "
                    "un nouveau snapshot."
                )
            )


    except Exception as exc:

        update_sheet_row_by_key(
            sheet_scans,
            "scan_id",
            scan_id,
            {

                "status":
                    "ERROR",

                "error_message":
                    str(exc),
            },
        )


        st.error(
            (
                "Le scan a rencontré "
                "une erreur, mais sa trace "
                "est conservée."
            )
        )


        st.code(
            str(exc)
        )


# ============================================================
# BUDGET API
# ============================================================

def get_budget_stats():

    scans = pd.DataFrame(
        sheet_scans.get_all_records()
    )


    stats = {

        "remaining":
            None,

        "today_used":
            0,

        "month_used":
            0,

        "avg_per_active_day":
            0,

        "usable_after_reserve":
            None,
    }


    if scans.empty:

        return stats


    if (
        "credits_used"
        in scans.columns
    ):

        scans[
            "credits_used_num"
        ] = (

            pd.to_numeric(
                scans[
                    "credits_used"
                ],
                errors="coerce",
            )
            .fillna(0)
        )

    else:

        scans[
            "credits_used_num"
        ] = 0


    if (
        "timestamp_paris"
        not in scans.columns
    ):

        return stats


    parsed = pd.to_datetime(
        scans[
            "timestamp_paris"
        ],
        format=(
            "%d/%m/%Y %H:%M:%S"
        ),
        errors="coerce",
    )


    scans[
        "_parsed"
    ] = parsed


    today = (
        now_paris().date()
    )


    current_month = (
        today.year,
        today.month,
    )


    today_mask = (

        scans[
            "_parsed"
        ].dt.date

        == today
    )


    month_mask = (

        (
            scans[
                "_parsed"
            ].dt.year

            == current_month[0]
        )

        &

        (
            scans[
                "_parsed"
            ].dt.month

            == current_month[1]
        )
    )


    stats[
        "today_used"
    ] = int(

        scans.loc[
            today_mask,
            "credits_used_num",
        ].sum()
    )


    stats[
        "month_used"
    ] = int(

        scans.loc[
            month_mask,
            "credits_used_num",
        ].sum()
    )


    active_days = (

        scans.loc[
            (
                month_mask
                &
                (
                    scans[
                        "credits_used_num"
                    ]
                    > 0
                )
            ),
            "_parsed",
        ]
        .dt.date
        .nunique()
    )


    if active_days:

        stats[
            "avg_per_active_day"
        ] = (

            stats[
                "month_used"
            ]

            / active_days
        )


    if (
        "credits_remaining"
        in scans.columns
    ):

        remaining_series = (

            pd.to_numeric(
                scans[
                    "credits_remaining"
                ],
                errors="coerce",
            )
            .dropna()
        )


        if (
            not remaining_series.empty
        ):

            stats[
                "remaining"
            ] = int(
                remaining_series.iloc[
                    -1
                ]
            )


    if (
        stats[
            "remaining"
        ]
        is not None
    ):

        stats[
            "usable_after_reserve"
        ] = max(

            0,

            stats[
                "remaining"
            ]
            - CREDIT_RESERVE,
        )


    return stats


def afficher_budget():

    stats = (
        get_budget_stats()
    )


    st.subheader(
        "💳 Budget API"
    )


    c1, c2, c3, c4 = (
        st.columns(4)
    )


    c1.metric(
        "Crédits restants",
        (
            stats[
                "remaining"
            ]

            if (
                stats[
                    "remaining"
                ]
                is not None
            )

            else "?"
        ),
    )


    c2.metric(
        "Utilisés aujourd'hui",
        stats[
            "today_used"
        ],
    )


    c3.metric(
        "Utilisés ce mois",
        stats[
            "month_used"
        ],
    )


    c4.metric(
        "Moyenne / jour actif",
        (
            f"{stats['avg_per_active_day']:.1f}"
        ),
    )


    if (
        stats[
            "remaining"
        ]
        is not None
    ):

        remaining = (
            stats[
                "remaining"
            ]
        )


        if remaining > 200:

            state = "🟢"

        elif remaining > 100:

            state = "🟠"

        else:

            state = "🔴"


        st.write(
            (
                f"{state} Réserve conseillée : "
                f"**{CREDIT_RESERVE} crédits**"
            )
        )


        st.write(
            (
                "Budget exploitable "
                "au-dessus de la réserve : "
                f"**{stats['usable_after_reserve']}**"
            )
        )


# ============================================================
# RESULTATS MANUELS
# ============================================================

def settle_signal_manual(
    signal_id,
    home_score,
    away_score
):

    signals = (
        load_signals_records()
    )


    signal = next(

        (
            row

            for row in signals

            if (
                str(
                    row.get(
                        "signal_id",
                        ""
                    )
                )

                == str(
                    signal_id
                )
            )
        ),

        None,
    )


    if signal is None:

        raise ValueError(
            "Signal introuvable."
        )


    home = signal[
        "home"
    ]

    away = signal[
        "away"
    ]


    if (
        home_score
        > away_score
    ):

        winner = home

    elif (
        away_score
        > home_score
    ):

        winner = away

    else:

        winner = "Draw"


    won = (

        str(
            signal[
                "outcome"
            ]
        )

        == str(
            winner
        )
    )


    bet_odd = float(
        signal.get(
            "bet_odd"
        )
        or 0
    )


    profit = round(

        (
            bet_odd - 1
            if won
            else -1
        ),

        4,
    )


    update_sheet_row_by_key(
        sheet_signals,
        "signal_id",
        signal_id,
        {

            "status":
                "SETTLED",

            "home_score":
                int(
                    home_score
                ),

            "away_score":
                int(
                    away_score
                ),

            "result":
                (
                    "WIN"
                    if won
                    else "LOSS"
                ),

            "profit":
                profit,
        },
    )


# ============================================================
# DASHBOARD
# ============================================================

def dashboard():

    signals = pd.DataFrame(
        sheet_signals.get_all_records()
    )


    if signals.empty:

        st.info(
            "Aucun signal officiel enregistré."
        )

        return


    for col in [
        "profit",
        "bet_odd",
        "edge_historical_pct",
        "clv_pct",
    ]:

        if col in signals.columns:

            signals[
                col
            ] = pd.to_numeric(
                signals[
                    col
                ],
                errors="coerce",
            )


    settled = signals[

        signals[
            "status"
        ]
        .astype(str)
        .str.upper()

        == "SETTLED"

    ].copy()


    open_df = signals[

        signals[
            "status"
        ]
        .astype(str)
        .str.upper()

        == "OPEN"

    ].copy()


    total_profit = (

        settled[
            "profit"
        ].sum()

        if not settled.empty

        else 0
    )


    roi = (

        total_profit
        / len(settled)
        * 100

        if len(settled)

        else 0
    )


    wins = (

        (
            settled[
                "result"
            ]
            == "WIN"
        ).sum()

        if len(settled)

        else 0
    )


    winrate = (

        wins
        / len(settled)
        * 100

        if len(settled)

        else 0
    )


    avg_edge = (

        signals[
            "edge_historical_pct"
        ].mean()

        if (
            "edge_historical_pct"
            in signals.columns
        )

        else float(
            "nan"
        )
    )


    avg_clv = (

        signals[
            "clv_pct"
        ]
        .dropna()
        .mean()

        if (
            "clv_pct"
            in signals.columns
        )

        else float(
            "nan"
        )
    )


    c1, c2, c3, c4 = (
        st.columns(4)
    )


    c1.metric(
        "Signals officiels",
        len(signals),
    )


    c2.metric(
        "Réglés",
        len(settled),
    )


    c3.metric(
        "Profit",
        f"{total_profit:+.2f} u",
    )


    c4.metric(
        "ROI",
        f"{roi:+.2f}%",
    )


    c1, c2, c3, c4 = (
        st.columns(4)
    )


    c1.metric(
        "Ouverts",
        len(open_df),
    )


    c2.metric(
        "Winrate",
        f"{winrate:.1f}%",
    )


    c3.metric(
        "Edge moyen",
        (
            f"{avg_edge:.2f}%"

            if pd.notna(
                avg_edge
            )

            else "—"
        ),
    )


    c4.metric(
        "CLV moyenne",
        (
            f"{avg_clv:+.2f}%"

            if pd.notna(
                avg_clv
            )

            else "—"
        ),
    )


    st.caption(
        (
            "Le ROI observé sur un petit "
            "échantillon peut varier fortement "
            "et ne garantit pas un rendement futur."
        )
    )


    if len(settled):

        st.subheader(
            "Performance par ligue"
        )


        by_league = (

            settled
            .groupby(
                "league"
            )
            .agg(

                paris=(
                    "signal_id",
                    "count",
                ),

                profit=(
                    "profit",
                    "sum",
                ),
            )
            .reset_index()
        )


        by_league[
            "roi_pct"
        ] = (

            by_league[
                "profit"
            ]

            / by_league[
                "paris"
            ]

            * 100
        )


        st.dataframe(
            by_league.sort_values(
                "paris",
                ascending=False,
            ),
            use_container_width=True,
            hide_index=True,
        )


    st.subheader(
        "Derniers signals"
    )


    st.dataframe(
        signals.tail(
            30
        ).iloc[::-1],
        use_container_width=True,
        hide_index=True,
    )


# ============================================================
# EXPORT
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
        engine="openpyxl",
    ) as writer:

        scans.to_excel(
            writer,
            sheet_name="Scans",
            index=False,
        )

        matchs.to_excel(
            writer,
            sheet_name="Matchs",
            index=False,
        )

        signals.to_excel(
            writer,
            sheet_name="Signals",
            index=False,
        )


    buffer.seek(0)

    return buffer


# ============================================================
# INTERFACE
# ============================================================

st.title(
    "📊 Value Scanner V8 Final"
)


st.caption(
    (
        "Planning + Closing Scanner économique + "
        "formule historique + quality check + ROI"
    )
)


st.info(
    (
        "🎯 Signal officiel = CLOSING 5–60 min "
        "+ edge historique ≥5 % "
        "+ contrôle qualité PASS."
    )
)


afficher_budget()


(
    tab_planning,
    tab_scan,
    tab_dashboard,
    tab_results,
    tab_history
) = st.tabs(
    [
        "📅 Planning",
        "🎯 Closing Scanner",
        "📈 Dashboard",
        "⚽ Résultats",
        "📚 Historique",
    ]
)


# ============================================================
# PLANNING
# ============================================================

with tab_planning:

    st.header(
        "📅 Matchs du jour"
    )


    selected_planning_leagues = (
        st.multiselect(

            "Ligues à surveiller",

            list(
                SPORTS.keys()
            ),

            default=list(
                SPORTS.keys()
            ),

            key="planning_leagues",
        )
    )


    if st.button(
        "🔄 Actualiser le planning",
        use_container_width=True,
    ):

        fetch_events_for_league.clear()


    (
        planning_rows,
        planning_errors
    ) = build_planning(
        selected_planning_leagues
    )


    today = (
        now_paris().date()
    )


    today_rows = [

        row

        for row in planning_rows

        if (
            row[
                "match_dt_paris"
            ].date()

            == today
        )
    ]


    st.metric(
        "Matchs encore à venir aujourd'hui",
        len(
            today_rows
        ),
    )


    closing_now = [

        row

        for row in today_rows

        if (
            row[
                "timing"
            ]
            == "CLOSING"
        )
    ]


    leagues_now = sorted(

        {
            row[
                "league"
            ]

            for row in closing_now
        }
    )


    if closing_now:

        st.success(
            (
                f"🎯 {len(closing_now)} match(s) "
                "sont actuellement en CLOSING."
            )
        )


        st.write(
            (
                "**Ligues à scanner maintenant :** "
                + ", ".join(
                    leagues_now
                )
            )
        )


        st.write(
            (
                "**Coût prévu du scan : "
                f"{len(leagues_now)} crédit(s)**"
            )
        )


    else:

        st.info(
            (
                "Aucun match n'est actuellement "
                "dans la fenêtre CLOSING."
            )
        )


    future = [

        row

        for row in today_rows

        if (
            row[
                "closing_start"
            ]
            > now_paris()
        )
    ]


    if future:

        next_start = min(

            row[
                "closing_start"
            ]

            for row in future
        )


        next_group = [

            row

            for row in future

            if abs(
                (
                    row[
                        "closing_start"
                    ]
                    - next_start
                ).total_seconds()
            )
            <= 600
        ]


        next_leagues = sorted(

            {
                row[
                    "league"
                ]

                for row in next_group
            }
        )


        minutes_until = (

            next_start
            - now_paris()

        ).total_seconds() / 60


        st.subheader(
            "⏰ Prochain scan recommandé"
        )


        st.write(
            (
                f"À partir de **"
                f"{next_start.strftime('%H:%M')}**, "
                f"dans environ **"
                f"{max(0, round(minutes_until))} min**."
            )
        )


        st.write(
            (
                "**Ligues :** "
                + ", ".join(
                    next_leagues
                )
            )
        )


        st.write(
            (
                "**Coût prévu : "
                f"{len(next_leagues)} crédit(s)**"
            )
        )


    if today_rows:

        planning_display = []


        for row in today_rows:

            planning_display.append({

                "Heure":
                    row[
                        "kickoff"
                    ],

                "Ligue":
                    row[
                        "league"
                    ],

                "Domicile":
                    row[
                        "home"
                    ],

                "Extérieur":
                    row[
                        "away"
                    ],

                "Fenêtre Closing":
                    row[
                        "closing_window"
                    ],

                "Minutes restantes":
                    round(
                        row[
                            "minutes_before_match"
                        ]
                    ),

                "Statut":
                    row[
                        "timing"
                    ],
            })


        st.dataframe(
            pd.DataFrame(
                planning_display
            ),
            use_container_width=True,
            hide_index=True,
        )


    if planning_errors:

        with st.expander(
            "⚠️ Erreurs planning"
        ):

            for error in planning_errors:

                st.write(
                    error
                )


# ============================================================
# CLOSING SCANNER
# ============================================================

with tab_scan:

    st.header(
        "🎯 Closing Scanner"
    )


    planning_rows, _ = (
        build_planning(
            list(
                SPORTS.keys()
            )
        )
    )


    today = (
        now_paris().date()
    )


    suggested_leagues = sorted(

        {
            row[
                "league"
            ]

            for row in planning_rows

            if (
                row[
                    "match_dt_paris"
                ].date()

                == today

                and

                row[
                    "timing"
                ]
                == "CLOSING"
            )
        }
    )


    selected_scan_leagues = (
        st.multiselect(

            "Ligues à scanner",

            list(
                SPORTS.keys()
            ),

            default=suggested_leagues,

            key="scan_leagues",
        )
    )


    scan_cost = len(
        selected_scan_leagues
    )


    if scan_cost == 0:

        st.info(
            (
                "Aucune ligue sélectionnée. "
                "Le scan coûtera 0 crédit."
            )
        )

    else:

        st.warning(
            (
                f"Coût prévu du prochain scan : "
                f"**{scan_cost} crédit(s)**."
            )
        )


    if st.button(
        "🔍 Scanner les cotes",
        type="primary",
        use_container_width=True,
    ):

        executer_scan(
            selected_scan_leagues
        )


# ============================================================
# DASHBOARD
# ============================================================

with tab_dashboard:

    st.header(
        "📈 Validation de la stratégie"
    )

    dashboard()


# ============================================================
# RESULTATS
# ============================================================

with tab_results:

    st.header(
        "⚽ Résultats manuels"
    )


    signals = (
        load_signals_records()
    )


    open_signals = [

        row

        for row in signals

        if (
            str(
                row.get(
                    "status",
                    ""
                )
            ).upper()

            == "OPEN"
        )
    ]


    if not open_signals:

        st.info(
            "Aucun signal ouvert."
        )


    else:

        labels = {}


        for row in open_signals:

            label = (
                f"{row['signal_id']} — "
                f"{row['home']} vs "
                f"{row['away']} — "
                f"{row['outcome']} "
                f"@{row['bet_odd']}"
            )


            labels[
                label
            ] = row


        selected_label = (
            st.selectbox(
                "Signal à régler",
                list(
                    labels.keys()
                ),
            )
        )


        selected_signal = (
            labels[
                selected_label
            ]
        )


        st.write(
            (
                f"**"
                f"{selected_signal['home']} "
                f"vs "
                f"{selected_signal['away']}"
                f"**"
            )
        )


        c1, c2 = (
            st.columns(2)
        )


        home_score = (
            c1.number_input(

                f"Buts "
                f"{selected_signal['home']}",

                min_value=0,

                step=1,
            )
        )


        away_score = (
            c2.number_input(

                f"Buts "
                f"{selected_signal['away']}",

                min_value=0,

                step=1,
            )
        )


        if st.button(
            "✅ Enregistrer le résultat",
            use_container_width=True,
        ):

            settle_signal_manual(
                selected_signal[
                    "signal_id"
                ],
                int(
                    home_score
                ),
                int(
                    away_score
                ),
            )


            st.success(
                "Résultat enregistré."
            )


            st.rerun()


# ============================================================
# HISTORIQUE
# ============================================================

with tab_history:

    st.header(
        "📚 Historique"
    )


    scans_df = pd.DataFrame(
        sheet_scans.get_all_records()
    )


    matchs_df = pd.DataFrame(
        sheet_matchs.get_all_records()
    )


    signals_df = pd.DataFrame(
        sheet_signals.get_all_records()
    )


    st.subheader(
        "Derniers scans"
    )


    if scans_df.empty:

        st.info(
            "Aucun scan enregistré."
        )

    else:

        st.dataframe(
            scans_df.tail(
                30
            ).iloc[::-1],
            use_container_width=True,
            hide_index=True,
        )


    st.subheader(
        "Derniers matchs CLOSING"
    )


    if matchs_df.empty:

        st.info(
            "Aucun match CLOSING enregistré."
        )

    else:

        st.dataframe(
            matchs_df.tail(
                60
            ).iloc[::-1],
            use_container_width=True,
            hide_index=True,
        )


    st.subheader(
        "Signals"
    )


    if signals_df.empty:

        st.info(
            "Aucun signal."
        )

    else:

        st.dataframe(
            signals_df.tail(
                50
            ).iloc[::-1],
            use_container_width=True,
            hide_index=True,
        )


    st.subheader(
        "📥 Export Excel"
    )


    try:

        st.download_button(
            "Télécharger l'historique Excel",
            data=creer_excel(),
            file_name=(
                "value_scanner_historique.xlsx"
            ),
            mime=(
                "application/vnd.openxmlformats-officedocument."
                "spreadsheetml.sheet"
            ),
        )

    except Exception as exc:

        st.warning(
            (
                "Export temporairement "
                f"indisponible : {exc}"
            )
        )
