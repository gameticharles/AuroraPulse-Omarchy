"""Extra links for stations, from the list the original app shipped.

The original AuroraPulse shipped a plain-text station list alongside the
radio-browser directory it browsed live. Every row in it is a station name
with up to six links, which is a very different shape from "one URL per
station" and is the only redundancy available for a stream whose listed link
has died.

It is also, measured by playing it, mostly dead: 14% of the stations that
appear *only* in that list still play, and 34% of the ones that also appear
in the mirror. So it is used for exactly one thing - offering a second and
third link for a station we already list - and never as a source of stations
in its own right. Importing its 30,000 unmatched names would have made the
catalogue look fuller while making it worse to use.
"""

import csv
import os
import re
import unicodedata

csv.field_size_limit(10 ** 7)

LINK_FIELDS = ("LINK1", "LINK2", "LINK3", "LINK4", "LINK5", "LINK6")
PLACEHOLDER = "-"


def stations_path(data_dir=None):
    """Where the bundled list lives inside an installed plugin."""
    if data_dir:
        candidate = os.path.join(data_dir, "stations.txt")
        if os.path.isfile(candidate):
            return candidate
    here = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))))
    candidate = os.path.join(here, "data", "stations.txt")
    return candidate if os.path.isfile(candidate) else None


def normalise(name):
    """A comparison key for station names.

    Loose on purpose but not loose enough to invent a match: the same station
    written "BBC Radio 1" and "BBC-Radio 1" has to land on the same key, while
    "Jazz FM" and "Jazz FM Live" must not.
    """
    text = unicodedata.normalize("NFKD", str(name or ""))
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text.lower())).strip()


def read(path=None):
    """Yield (name, [links], country, genre) for every usable row."""
    path = path or stations_path()
    if not path or not os.path.isfile(path):
        return
    with open(path, newline="", encoding="utf-8", errors="replace") as handle:
        for row in csv.DictReader(handle, delimiter="\t", quotechar='"'):
            name = (row.get("STATION") or "").strip()
            links = []
            for field in LINK_FIELDS:
                value = (row.get(field) or "").strip()
                if value and value != PLACEHOLDER and value not in links:
                    links.append(value)
            if name and links:
                yield (name, links,
                       (row.get("COUNTRY") or "").strip()[:80],
                       (row.get("GENRE") or "").strip()[:200])


def links_for_mirror(path=None):
    """Map normalised station name -> links, for stations the mirror has.

    Only names we actually hold produce entries, so the caller can write the
    result straight into the alternate-link table.
    """
    known = {}
    for name, links, _country, _genre in read(path):
        key = normalise(name)
        if key and key not in known:
            known[key] = links
    return known
