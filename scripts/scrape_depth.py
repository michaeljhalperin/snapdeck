#!/usr/bin/env python3
"""
Snap Deck - Ourlads depth chart scraper.

Reads the all-teams depth chart and writes data/ourlads.txt.

The output is deliberately the SAME tab-separated text you get by selecting
the page in a browser and copying it. loadDepth() in index.html is not
changed and does not know the difference: team header lines, "Offense - X"
section lines, then TAB-separated rows of
    TEAM  Pos  No.  Player1  No  Player2  ...
That keeps the fragile parsing in one place instead of two.

Like the injury scraper, this refuses to write a file it doesn't trust,
so a site redesign leaves last week's good chart in place.
"""

import os
import re
import sys
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

URL = "https://www.ourlads.com/nfldepthcharts/depthcharts.aspx"
OUT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "ourlads.txt",
)

# Ourlads' own codes, including the two that differ from the CSV's.
OL_TEAMS = {
    "ARZ", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE", "DAL", "DEN",
    "DET", "GB", "HOU", "IND", "JAX", "KC", "LAC", "LV", "MIA", "MIN",
    "NE", "NO", "NYG", "NYJ", "PHI", "PIT", "RAM", "SEA", "SF", "TB",
    "TEN", "WAS",
}

SECTION_RE = re.compile(
    r"^(Offense|Defense|Special Teams|Practice Squad|Reserves)\s*-", re.I)
UPDATED_RE = re.compile(r"^(.+?)\s+Updated:\s*(\S+)")

MIN_TEAMS = 24     # 32 normally; refuse to write if the page half-loaded
MIN_ROWS = 500


def clean(node):
    """Cell text with newlines and doubled spaces flattened out."""
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()


def scrape(html):
    soup = BeautifulSoup(html, "lxml")
    lines, teams_seen, row_count = [], set(), 0

    # Walk every row on the page in document order and classify by content
    # rather than by CSS class, so a restyle doesn't break this.
    for tr in soup.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if not cells:
            continue
        texts = [clean(c) for c in cells]
        first = texts[0]

        # "Arizona Cardinals Updated: 09/02/2026 8:14PM" - may share a row
        # with the team logo cell, so check every cell.
        header = None
        for t in texts:
            m = UPDATED_RE.match(t)
            if m and len(m.group(1)) > 3:
                header = t
                break
        if header:
            lines.append(header)
            continue

        if SECTION_RE.match(first):
            lines.append(first)
            continue

        if first in OL_TEAMS:
            # Emit the row verbatim as tabs. Trailing empty cells are kept:
            # parsePlayerCell ignores blanks, and dropping them would shift
            # the column index that decides depth order.
            lines.append("\t".join(texts))
            teams_seen.add(first)
            row_count += 1

    return "\n".join(lines) + "\n", teams_seen, row_count


def fetch(url, tries=3):
    headers = {
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0.0.0 Safari/537.36"),
        "Accept-Language": "en-US,en;q=0.9",
    }
    last = None
    for attempt in range(1, tries + 1):
        try:
            r = requests.get(url, headers=headers, timeout=45)
            r.raise_for_status()
            return r.text
        except Exception as exc:          # noqa: BLE001
            last = exc
            print(f"  attempt {attempt}/{tries} failed: {exc}", file=sys.stderr)
    raise SystemExit(f"Could not fetch {url}: {last}")


def main():
    print(f"Fetching {URL}")
    text, teams, rows = scrape(fetch(URL))
    print(f"  {len(teams)} teams, {rows} chart rows, {len(text)} chars")

    if len(teams) < MIN_TEAMS or rows < MIN_ROWS:
        raise SystemExit(
            f"Only found {len(teams)} teams / {rows} rows "
            f"(expected >= {MIN_TEAMS} / {MIN_ROWS}).\n"
            "Ourlads' page structure probably changed. Refusing to overwrite\n"
            "data/ourlads.txt - the existing chart is left in place."
        )

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(f"Wrote {OUT_PATH} at "
          f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}")


if __name__ == "__main__":
    main()
