#!/usr/bin/env python3
"""
Snap Deck - NFL.com injury report scraper.

Reads https://www.nfl.com/injuries/ and writes data/injuries.json.

Design notes (read before editing):

* Team abbreviations are emitted in the SNAP-COUNT CSV convention
  (ARI, LAR, ...), never NFL.com's own (AZ) and never Ourlads' (ARZ, RAM).
  That way this file plugs into the same normalized space the rest of
  Snap Deck already works in.

* Teams are identified by nickname text sitting above each table rather
  than by CSS class, so an NFL.com redesign of class names won't break it.

* It refuses to write a file it doesn't trust. If the page structure
  stops making sense, it exits non-zero and leaves the previous
  injuries.json untouched, so Snap Deck shows slightly stale data
  instead of silently showing an empty injury report.
"""

import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup, NavigableString

URL = "https://www.nfl.com/injuries/"
OUT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "injuries.json",
)

# Nickname -> abbreviation, in the CSV's convention.
TEAMS = {
    "cardinals": "ARI", "falcons": "ATL", "ravens": "BAL", "bills": "BUF",
    "panthers": "CAR", "bears": "CHI", "bengals": "CIN", "browns": "CLE",
    "cowboys": "DAL", "broncos": "DEN", "lions": "DET", "packers": "GB",
    "texans": "HOU", "colts": "IND", "jaguars": "JAX", "chiefs": "KC",
    "chargers": "LAC", "rams": "LAR", "raiders": "LV", "dolphins": "MIA",
    "vikings": "MIN", "patriots": "NE", "saints": "NO", "giants": "NYG",
    "jets": "NYJ", "eagles": "PHI", "steelers": "PIT", "seahawks": "SEA",
    "49ers": "SF", "buccaneers": "TB", "titans": "TEN", "commanders": "WAS",
}

SUFFIXES = {"JR", "SR", "II", "III", "IV", "V"}

# If we can't confirm we understood at least this many team blocks,
# assume the page changed and bail rather than writing garbage.
MIN_TEAM_BLOCKS = 8


def norm_key(name):
    """Convenience match key: no accents, no punctuation, no suffix, upper.

    Snap Deck already normalizes names its own way. This is only here so
    you have a fallback key; prefer running your own normalizer on `name`.
    """
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^A-Za-z0-9 ]", " ", s)
    parts = [p for p in s.upper().split() if p]
    while len(parts) > 1 and parts[-1] in SUFFIXES:
        parts.pop()
    return " ".join(parts)


def clean(el):
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip() if el else ""


def team_above(node):
    """Nearest team nickname appearing before this node in the document."""
    for prev in node.previous_elements:
        if isinstance(prev, NavigableString):
            text = str(prev).strip().lower()
            if text in TEAMS:
                return TEAMS[text]
    return None


def parse(html):
    soup = BeautifulSoup(html, "lxml")

    week = season = None
    title = clean(soup.find("title"))
    m = re.search(r"week\s+(\d+)\s+of\s+the\s+(\d{4})", title, re.I)
    if m:
        week, season = int(m.group(1)), int(m.group(2))

    players = []
    reported = set()      # teams whose block we successfully understood
    blocks = 0

    for table in soup.find_all("table"):
        headers = [clean(th).lower() for th in table.find_all("th")]
        if not any(h.startswith("player") for h in headers):
            continue

        team = team_above(table)
        if not team:
            continue

        blocks += 1
        reported.add(team)

        def col(name):
            for i, h in enumerate(headers):
                if h.startswith(name):
                    return i
            return None

        i_player = col("player")
        i_pos = col("position")
        i_inj = col("injur")
        i_prac = col("practice")
        i_game = col("game")

        body = table.find("tbody") or table
        for row in body.find_all("tr"):
            cells = row.find_all("td")
            if not cells:
                continue

            def cell(i):
                return clean(cells[i]) if i is not None and i < len(cells) else ""

            name = cell(i_player)
            if not name:
                continue

            players.append({
                "team": team,
                "name": name,
                "key": norm_key(name),
                "pos": cell(i_pos),
                "injury": cell(i_inj),
                "practice": cell(i_prac),
                "status": cell(i_game),
            })

    # Teams that posted a report with nobody on it still count as
    # "understood" - that's a meaningful signal, not missing data.
    for tag in soup.find_all(string=re.compile(r"No Injuries Reported", re.I)):
        team = team_above(tag)
        if team:
            blocks += 1
            reported.add(team)

    return {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": URL,
        "season": season,
        "week": week,
        "teams_reported": sorted(reported),
        "players": players,
    }, blocks


def fetch(url, tries=3):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
    last = None
    for attempt in range(1, tries + 1):
        try:
            r = requests.get(url, headers=headers, timeout=30)
            r.raise_for_status()
            return r.text
        except Exception as exc:          # noqa: BLE001
            last = exc
            print(f"  attempt {attempt}/{tries} failed: {exc}", file=sys.stderr)
    raise SystemExit(f"Could not fetch {url}: {last}")


def main():
    print(f"Fetching {URL}")
    data, blocks = parse(fetch(URL))

    print(f"  understood {blocks} team blocks, "
          f"{len(data['teams_reported'])} teams, "
          f"{len(data['players'])} listed players")

    if blocks < MIN_TEAM_BLOCKS:
        raise SystemExit(
            f"Only understood {blocks} team blocks (expected >= {MIN_TEAM_BLOCKS}).\n"
            "NFL.com's page structure probably changed. Refusing to overwrite\n"
            "data/injuries.json - the existing file is left in place."
        )

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, ensure_ascii=False)
        fh.write("\n")

    print(f"Wrote {OUT_PATH} (week {data['week']}, season {data['season']})")


if __name__ == "__main__":
    main()
