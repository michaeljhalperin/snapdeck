#!/usr/bin/env python3
"""
Snap Deck - anytime TD scorer odds, sportsbooks and exchanges.

Writes data/odds.json.

Cost model (the whole reason for the knobs below):
  credits = markets x regions, PER GAME, because props use the per-event
  endpoint. One market, one region, one game = 1 credit.
    us only, 16 games ....... 16 credits
    us + us_ex, 16 games .... 32 credits
  The free tier is 500/month, so a full slate with both regions three
  times a week would eat ~415. Two scheduled runs plus targeted
  single-game refreshes is the sustainable pattern.

Controls, all optional environment variables:
  ODDS_API_KEY   required
  ODDS_REGIONS   default "us,us_ex". Use "us" to halve the cost.
  ONLY_TEAMS     e.g. "SEA,NE" - fetch only games involving these teams.
                 Anything not fetched KEEPS its existing prices; a
                 targeted refresh merges rather than replacing the file.
  DAYS_AHEAD     default 8

Exchanges (Novig, ProphetX, Kalshi, Polymarket, BetOpenly) are reported
separately from sportsbooks. Their prices carry little or no vig, so
exImplied is much closer to a true probability than the sportsbook
median, which still has juice in it.
"""

import json
import os
import re
import statistics
import sys
import unicodedata
from datetime import datetime, timezone, timedelta

import requests

API = "https://api.the-odds-api.com/v4"
SPORT = "americanfootball_nfl"
MARKET = "player_anytime_td"
MAX_EVENTS = 20

OUT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "odds.json",
)

# Bookmaker keys the API classifies under the us_ex region.
EXCHANGES = {"betopenly", "kalshi", "novig", "polymarket", "prophetx"}

TEAMS = {
    "Arizona Cardinals": "ARI", "Atlanta Falcons": "ATL",
    "Baltimore Ravens": "BAL", "Buffalo Bills": "BUF",
    "Carolina Panthers": "CAR", "Chicago Bears": "CHI",
    "Cincinnati Bengals": "CIN", "Cleveland Browns": "CLE",
    "Dallas Cowboys": "DAL", "Denver Broncos": "DEN",
    "Detroit Lions": "DET", "Green Bay Packers": "GB",
    "Houston Texans": "HOU", "Indianapolis Colts": "IND",
    "Jacksonville Jaguars": "JAX", "Kansas City Chiefs": "KC",
    "Las Vegas Raiders": "LV", "Los Angeles Chargers": "LAC",
    "Los Angeles Rams": "LAR", "Miami Dolphins": "MIA",
    "Minnesota Vikings": "MIN", "New England Patriots": "NE",
    "New Orleans Saints": "NO", "New York Giants": "NYG",
    "New York Jets": "NYJ", "Philadelphia Eagles": "PHI",
    "Pittsburgh Steelers": "PIT", "San Francisco 49ers": "SF",
    "Seattle Seahawks": "SEA", "Tampa Bay Buccaneers": "TB",
    "Tennessee Titans": "TEN", "Washington Commanders": "WAS",
}

SUFFIXES = {"JR", "SR", "II", "III", "IV", "V"}
DST_RE = re.compile(r"\b(D/ST|Defense|Special Teams)\b", re.I)


def norm_key(name):
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^A-Za-z0-9 ]", " ", s)
    parts = [p for p in s.upper().split() if p]
    while len(parts) > 1 and parts[-1] in SUFFIXES:
        parts.pop()
    return " ".join(parts)


def to_decimal(american):
    a = float(american)
    return 1 + (a / 100.0 if a > 0 else 100.0 / abs(a))


def get(path, key, **params):
    params["apiKey"] = key
    r = requests.get(f"{API}{path}", params=params, timeout=30)
    if r.status_code != 200:
        raise SystemExit(f"{path} returned HTTP {r.status_code}: {r.text[:300]}")
    return (r.json(), r.headers.get("x-requests-remaining"),
            r.headers.get("x-requests-used"))


def load_existing():
    """Previous file, so a targeted refresh doesn't wipe other games."""
    try:
        with open(OUT_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:                          # noqa: BLE001
        return {"events": [], "players": []}


def summarise(rec, titles):
    books = {b: p for b, p in rec["prices"].items() if b not in EXCHANGES}
    exch = {b: p for b, p in rec["prices"].items() if b in EXCHANGES}
    out = {"key": rec["key"], "name": rec["name"], "teams": rec["teams"],
           "event": rec["event"]}

    if books:
        bb, bp = max(books.items(), key=lambda kv: to_decimal(kv[1]))
        out["best"] = bp
        out["bestBook"] = titles.get(bb, bb)
        # Every book's price, longest first, so the detail panel can show the
        # whole board instead of just the winner.
        out["prices"] = {titles.get(b, b): p for b, p in
                         sorted(books.items(), key=lambda kv: -to_decimal(kv[1]))}
        # Vig included: the API returns only the Yes side, so there is no No
        # price to devig against. A market price, not a true probability.
        out["implied"] = round(
            statistics.median(1 / to_decimal(p) for p in books.values()), 4)
        out["books"] = len(books)

    if exch:
        eb, ep = max(exch.items(), key=lambda kv: to_decimal(kv[1]))
        out["ex"] = ep
        out["exBook"] = titles.get(eb, eb)
        out["exPrices"] = {titles.get(b, b): p for b, p in
                           sorted(exch.items(), key=lambda kv: -to_decimal(kv[1]))}
        # Little to no vig here, so this is the closer read on true odds.
        out["exImplied"] = round(
            statistics.median(1 / to_decimal(p) for p in exch.values()), 4)
        out["exBooks"] = len(exch)

    return out


def main():
    key = os.environ.get("ODDS_API_KEY", "").strip()
    if not key:
        raise SystemExit("ODDS_API_KEY is not set. Add it as a repository secret.")

    regions = (os.environ.get("ODDS_REGIONS") or "us,us_ex").strip()
    only = {t.strip().upper()
            for t in (os.environ.get("ONLY_TEAMS") or "").split(",") if t.strip()}
    days = int(os.environ.get("DAYS_AHEAD") or 8)
    per_game = len([r for r in regions.split(",") if r.strip()])

    events, left, used = get(f"/sports/{SPORT}/events", key)
    cutoff = datetime.now(timezone.utc) + timedelta(days=days)

    soon = []
    for e in events:
        try:
            when = datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00"))
        except Exception:                      # noqa: BLE001
            continue
        if when > cutoff:
            continue
        home = TEAMS.get(e.get("home_team", ""))
        away = TEAMS.get(e.get("away_team", ""))
        if not home or not away:
            print(f"  ! unknown team: {e.get('away_team')} @ {e.get('home_team')}")
            continue
        if only and not (only & {home, away}):
            continue
        e["_home"], e["_away"] = home, away
        soon.append(e)
    soon = soon[:MAX_EVENTS]

    print(f"regions={regions}  ({per_game} credit(s) per game)")
    if only:
        print(f"targeted refresh: {','.join(sorted(only))}")
    print(f"{len(soon)} game(s) to fetch = about {len(soon) * per_game} credits")
    print(f"  credits remaining: {left} (used {used})")
    if not soon:
        raise SystemExit("No matching games. Nothing fetched, existing file untouched.")

    fetched, ok_events, titles = {}, [], {}
    for e in soon:
        home, away = e["_home"], e["_away"]
        try:
            data, left, used = get(
                f"/sports/{SPORT}/events/{e['id']}/odds", key,
                regions=regions, markets=MARKET, oddsFormat="american")
        except SystemExit as exc:
            print(f"  ! {away}@{home}: {exc}", file=sys.stderr)
            continue

        n = 0
        for bk in data.get("bookmakers", []):
            bkey = (bk.get("key") or "").lower()
            titles[bkey] = bk.get("title") or bkey
            for mkt in bk.get("markets", []):
                if mkt.get("key") != MARKET:
                    continue
                for out in mkt.get("outcomes", []):
                    name = (out.get("description") or "").strip()
                    if not name or DST_RE.search(name) or out.get("price") is None:
                        continue
                    k = norm_key(name)
                    rec = fetched.setdefault(k, {
                        "key": k, "name": name, "teams": [away, home],
                        "event": e["id"], "prices": {}})
                    prev = rec["prices"].get(bkey)
                    if prev is None or to_decimal(out["price"]) > to_decimal(prev):
                        rec["prices"][bkey] = out["price"]
                    n += 1

        ex_n = sum(1 for r in fetched.values() if r["event"] == e["id"]
                   and any(b in EXCHANGES for b in r["prices"]))
        print(f"  {away}@{home}: {n} prices, {ex_n} players with exchange lines")
        if n:
            ok_events.append({
                "id": e["id"], "away": away, "home": home,
                "commence": e["commence_time"],
                "refreshed": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})

    if not ok_events:
        raise SystemExit(
            "No game returned any anytime-TD prices. Refusing to overwrite\n"
            "data/odds.json - the previous file is left in place.")

    # Merge: replace only the games just refreshed, keep everything else.
    prev = load_existing()
    refreshed_ids = {e["id"] for e in ok_events}
    kept_players = [p for p in prev.get("players", [])
                    if p.get("event") not in refreshed_ids]
    kept_events = [e for e in prev.get("events", [])
                   if e.get("id") not in refreshed_ids]

    new_players = [summarise(r, titles) for r in fetched.values()]
    players = kept_players + new_players
    players.sort(key=lambda p: -(p.get("exImplied") or p.get("implied") or 0))

    payload = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "market": MARKET,
        "regions": regions,
        "creditsRemaining": left,
        "events": kept_events + ok_events,
        "players": players,
    }

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1, ensure_ascii=False)
        fh.write("\n")

    with_ex = sum(1 for p in new_players if "ex" in p)
    print(f"\nRefreshed {len(ok_events)} game(s): {len(new_players)} players "
          f"({with_ex} with exchange prices)")
    print(f"Kept {len(kept_players)} players from {len(kept_events)} untouched game(s)")
    print(f"  credits remaining: {left} (used {used})")


if __name__ == "__main__":
    main()
