import streamlit as st
import requests
from datetime import datetime, timezone

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

def analyser_match(match, league_name):
    home = match["home_team"]
    away = match["away_team"]

    match_time = datetime.fromisoformat(
        match["commence_time"].replace("Z", "+00:00")
    )

    now = datetime.now(timezone.utc)

    minutes_before_match = (
        match_time - now
    ).total_seconds() / 60

    if minutes_before_match <= 0:
        return []

    timing_type = (
        "CLOSING"
        if minutes_before_match <= 60
        else "EARLY"
    )

    bookmaker_probs = []

    best = {
        home: {"odd": 0, "book": None},
        "Draw": {"odd": 0, "book": None},
        away: {"odd": 0, "book": None},
    }

    for bookmaker in match.get("bookmakers", []):
        book_name = bookmaker.get("title", "Inconnu")

        for market in bookmaker.get("markets", []):
            if market.get("key") != "h2h":
                continue

            odds = {
                outcome["name"]: outcome["price"]
                for outcome in market.get("outcomes", [])
            }

            if home not in odds or away not in odds:
                continue

            draw_key = next(
                (x for x in odds if x.lower() == "draw"),
                None
            )

            if draw_key is None:
                continue

            c1 = odds[home]
            cx = odds[draw_key]
            c2 = odds[away]

            p1, px, p2 = demarger(c1, cx, c2)

            bookmaker_probs.append((p1, px, p2))

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
        away: 1 / consensus_away,
    }

    signals = []

    for outcome in [home, "Draw", away]:
        max_odd = best[outcome]["odd"]

        if max_odd == 0:
            continue

        fair_odd = fair[outcome]

        edge = (max_odd / fair_odd) - 1

        if edge >= EDGE_MIN:
            signals.append({
                "league": league_name,
                "home": home,
                "away": away,
                "outcome": outcome,
                "fair_odd": fair_odd,
                "best_odd": max_odd,
                "bookmaker": best[outcome]["book"],
                "edge": edge,
                "minutes_before_match": minutes_before_match,
                "timing": timing_type
            })

    return signals

def scanner_ligue(league_name, sport_key):
    url = (
        f"https://api.the-odds-api.com/v4/sports/"
        f"{sport_key}/odds"
    )

    params = {
        "apiKey": API_KEY,
        "regions": "fr",
        "markets": "h2h",
        "oddsFormat": "decimal",
    }

    response = requests.get(
        url,
        params=params,
        timeout=20
    )

    if response.status_code != 200:
        return [], None, response.text

    remaining = response.headers.get(
        "x-requests-remaining"
    )

    matches = response.json()

    signals = []

    for match in matches:
        signals.extend(
            analyser_match(match, league_name)
        )

    return signals, remaining, None

st.title("📊 Value Scanner")
st.caption("Consensus bookmaker → Fair odds → Edge ≥ 5%")

selected_leagues = st.multiselect(
    "Ligues à scanner",
    list(SPORTS.keys()),
    default=list(SPORTS.keys())
)

if st.button("🔍 Scanner maintenant", use_container_width=True):

    all_signals = []
    credits_remaining = None

    with st.spinner("Scan en cours..."):

        for league_name in selected_leagues:
            sport_key = SPORTS[league_name]

            signals, remaining, error = scanner_ligue(
                league_name,
                sport_key
            )

            if error:
                st.warning(
                    f"{league_name} : erreur API"
                )
                continue

            if remaining is not None:
                credits_remaining = remaining

            all_signals.extend(signals)

    if credits_remaining is not None:
        st.info(
            f"Crédits API restants : "
            f"{credits_remaining}"
        )

    if not all_signals:
        st.success(
            "Aucune value ≥ 5 % détectée."
        )

    else:
        st.success(
            f"{len(all_signals)} value(s) détectée(s)"
        )

        for s in sorted(
            all_signals,
            key=lambda x: x["edge"],
            reverse=True
        ):

            st.subheader(
                f"{s['home']} vs {s['away']}"
            )

            st.write(
                f"**Ligue :** {s['league']}"
            )

            st.write(
                f"**Marché :** {s['outcome']}"
            )

            st.write(
                f"**Fair odd :** "
                f"{s['fair_odd']:.2f}"
            )

            st.write(
                f"**Meilleure cote :** "
                f"{s['best_odd']:.2f}"
            )

            st.write(
                f"**Bookmaker :** "
                f"{s['bookmaker']}"
            )

            st.write(
                f"**Edge : +"
                f"{s['edge']*100:.2f}%**"
            )

            st.write(
                f"**Timing :** "
                f"{s['timing']} — "
                f"{s['minutes_before_match']:.0f} min"
            )

            st.divider()

