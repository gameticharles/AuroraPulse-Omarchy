"""Internet radio, from the community directory.

Radio Browser is a federated set of mirrors holding the same database, so we
mirror-fail over the list rather than trusting any single host. Play
reporting is opt-out and anonymous: it is a single UUID we generate once, and
the setting can turn it off entirely.
"""

import os
import time
import uuid

from ..util import health, netguard, taxonomy, textutil
from .base import (SourceError, art_for, cached, fetch_json, item, throttle)

# Set by the daemon: the background artwork fetcher. Sources never download
# anything inline.
FETCHER = [None]


def set_art_fetcher(fetcher):
    FETCHER[0] = fetcher

# The local mirror, when the daemon has opened one. Set by core/daemon.py; the
# sources must work without it, because the daemon may be running before the
# first sync finishes.
_CATALOGUE = None


def set_catalogue(catalogue):
    global _CATALOGUE
    _CATALOGUE = catalogue

_last_mirror = {"index": 0, "tried": []}

MIRRORS = [
    "https://de1.api.radio-browser.info",
    "https://de2.api.radio-browser.info",
    "https://fr1.api.radio-browser.info",
    "https://at1.api.radio-browser.info",
    "https://nl1.api.radio-browser.info",
    "https://no1.api.radio-browser.info",
]

# The server the user picked in settings, or "" for automatic. The same
# choice the original app offered: a pinned server is tried first, and the
# others are still there as a fallback if it is down.
_SERVER = [""]


def set_server(url):
    url = (url or "").strip().rstrip("/")
    if url.endswith("/json"):
        url = url[:-5]
    _SERVER[0] = url if url.startswith("https://") else ""
    if _SERVER[0] in MIRRORS:
        _last_mirror["index"] = MIRRORS.index(_SERVER[0])


def api_base():
    """The Radio Browser host to talk to right now."""
    return _SERVER[0] or MIRRORS[_last_mirror["index"] % len(MIRRORS)]

# A small hand-picked set so the plugin is useful on the first launch, before
# any search has happened. Public streams only, and all long-lived.
CURATED = [
    ("SomaFM Groove Salad", "https://ice5.somafm.com/groovesalad-128-mp3",
     "Ambient", "https://somafm.com/img3/gr-256.jpg"),
    ("SomaFM Drone Zone", "https://ice2.somafm.com/dronezone-128-mp3",
     "Ambient", "https://somafm.com/img3/dz-256.jpg"),
    ("SomaFM Secret Agent", "https://ice2.somafm.com/secretagent-128-mp3",
     "Lounge", "https://somafm.com/img3/sa-256.jpg"),
    ("SomaFM Indie Pop Rocks", "https://ice2.somafm.com/indiepop-128-mp3",
     "Indie", "https://somafm.com/img3/ip-256.jpg"),
    ("SomaFM Metal Detector", "https://ice2.somafm.com/metal-128-mp3",
     "Metal", "https://somafm.com/img3/md-256.jpg"),
    ("FIP", "https://icecast.radiofrance.fr/fip-midfi.mp3", "Eclectic", ""),
    ("KEXP 90.3", "https://kexp-mp3-128.streamguys1.com/kexp128.mp3",
     "Indie", ""),
    ("WNYC 93.9", "https://fm939.wnyc.org/wnycfm", "Eclectic", ""),
    ("Nightwave Plaza", "https://radio.plaza.one/mp3", "Vaporwave", ""),
    ("Radio Paradise Main", "https://stream.radioparadise.com/mp3-192",
     "Eclectic", ""),
    ("Jazz24", "https://live.wostreaming.net/direct/ppm-jazz24mp3-ibc1",
     "Jazz", ""),
    ("KCRW Eclectic24", "https://kcrw.streamguys1.com/kcrw_192k_mp3_e",
     "Eclectic", ""),
]



def _mirror_get(path, **params):
    """Try each mirror in turn. The last error is the one worth reporting."""
    from urllib.parse import urlencode
    query = ("?" + urlencode(params)) if params else ""
    last = None
    order = list(range(len(MIRRORS)))
    start = _last_mirror["index"]
    order = order[start:] + order[:start]
    for index in order:
        base = MIRRORS[index]
        try:
            data = fetch_json(base + path + query, limit=6 << 20, seconds=15)
            _last_mirror["index"] = index
            return data
        except SourceError as exc:
            last = exc
            continue
    raise last or SourceError("every Radio Browser mirror failed", "network")


def _station(entry, cache_dir=None, min_bitrate=0):
    """Normalise one directory entry.

    Radio Browser hands back everything from 8 kbps AM to 320 kbps FLAC, and
    some entries have an empty or broken stream URL. Those are dropped here
    rather than becoming a silent failure later.
    """
    url = (entry.get("url_resolved") or entry.get("url") or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return None
    # A station the directory has already given up on, with no working
    # alternate, is a row that can only ever fail.
    if not entry.get("url_resolved") and not health.checked_flag(
            entry.get("lastcheckok"), 1):
        return None

    name = textutil.text(entry.get("name") or "Unknown station", 200)
    tags = [textutil.text(t, 40) for t in (entry.get("tags") or []) if t][:4]
    genre = tags[0] if tags else textutil.text(entry.get("genre"), 60)
    bitrate = int(entry.get("bitrate") or 0) or 0
    if min_bitrate and bitrate and bitrate < min_bitrate:
        return None

    station_id = str(entry.get("stationuuid") or name)
    favicon = (entry.get("favicon") or "").strip()
    return item(
        "radio", station_id, name, url=url,
        kind="station",
        artist=genre,
        bitrate=bitrate,
        codec=textutil.text(entry.get("codec"), 32).upper(),
        is_live=True,
        art=art_for(favicon, "radio-%s" % station_id, FETCHER[0]),
        artKey="radio-%s" % station_id,
        extra={
            "genre": genre,
            "tags": ", ".join(tags),
            "country": textutil.text(entry.get("country"), 60),
            "state": textutil.text(entry.get("state"), 60),
            "votes": int(entry.get("votes") or 0),
            "homepage": textutil.text(entry.get("homepage"), 300),
            "codec": textutil.text(entry.get("codec"), 32).upper(),
        },
    )


def _from_row(row, cache_dir=None):
    """A mirrored row as a MediaItem, identical to the network path's shape."""
    station_id = row["uuid"]
    return item(
        "radio", station_id, row["name"], url=row["url_resolved"] or row["url"],
        kind="station",
        artist=(row["tags"] or "").split(",")[0].strip() if row["tags"] else "",
        bitrate=int(row["bitrate"] or 0),
        codec=(row["codec"] or "").upper(),
        is_live=True,
        art=art_for(row["favicon"], "radio-%s" % station_id, FETCHER[0]),
        artKey="radio-%s" % station_id,
        extra={
            "genre": (row["tags"] or "").split(",")[0].strip() if row["tags"] else "",
            "tags": (row["tags"] or "")[:400],
            "country": row["country"] or "",
            "state": row["state"] or "",
            "votes": int(row["votes"] or 0),
            "homepage": row["homepage"] or "",
            "codec": (row["codec"] or "").upper(),
            "language": row["language"] or "",
        },
    )


def _mirrored():
    """Has the full station list been downloaded and kept?

    Once it has, this tab answers from disk and nothing else. The network is a
    fallback for a first run that has not downloaded yet, not a second opinion:
    a list that quietly changes depending on which host answered last is not a
    list anyone can use, and the status bar can only be honest about "last
    updated" if the rows underneath it stop moving.
    """
    if _CATALOGUE is None:
        return False
    try:
        return bool(_CATALOGUE.mirrored("radio"))
    except Exception:
        return False


def _local(query, limit, min_bitrate, cache_dir, order="votes", offset=0,
           country="", genre=""):
    """Answer from the mirror. Returns None when it cannot, so the caller
    falls back to the network rather than showing an empty list.

    The country and genre filters are applied here, in SQL. Doing it in Python
    after the fact would mean fetching a page and then throwing most of it
    away, so a narrow filter would show fewer stations per page than a wide
    one and the pager would end early.
    """
    if _CATALOGUE is None:
        return None
    try:
        rows = _CATALOGUE.search_radio(term=query, limit=limit, offset=offset,
                                       min_bitrate=min_bitrate, order=order,
                                       country=country or None,
                                       group=genre or None)
    except Exception:
        return None
    if not rows and not offset:
        return None
    out = []
    for row in rows:
        built = _from_row(row, cache_dir)
        if built:
            out.append(built)
    return out


def _answer_locally(query, limit, min_bitrate, cache_dir, order="votes",
                    country="", genre="", offset=0):
    """Rows from the mirror, with an empty list meaning 'nothing matched'.

    Distinguishes "the mirror has no answer" from "the mirror says no", which
    is what decides whether reaching for the network is legitimate at all.
    """
    local = _local(query, limit, min_bitrate, cache_dir, order=order,
                   offset=offset, country=country, genre=genre)
    if local:
        return local
    # A later page of the network fallback would repeat the first one.
    return [] if (_mirrored() or offset) else None


def search(query, limit=50, min_bitrate=0, cache_dir=None, country="",
           genre="", offset=0):
    query = textutil.text(query, 120)
    if not query:
        local = _answer_locally("", limit, min_bitrate, cache_dir,
                                order="votes", country=country, genre=genre,
                                offset=offset)
        if local is not None:
            return local
        return curated(cache_dir, min_bitrate)
    local = _answer_locally(query, limit, min_bitrate, cache_dir,
                            country=country, genre=genre, offset=offset)
    if local is not None:
        return local
    try:
        raw, _age = cached("radio:search:%s" % query.lower(), lambda: _mirror_get(
            "/json/stations/search", name=query, limit=min(500, limit * 3),
            hidebroken="true", order="votes", reverse="true"), ttl=600)
    except SourceError:
        raise
    if not isinstance(raw, list):
        raise SourceError("the directory sent something unexpected", "network")
    out = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        station = _station(entry, cache_dir, min_bitrate)
        if station:
            out.append(station)
        if len(out) >= limit:
            break
    return out


def browse(section="popular", limit=50, min_bitrate=0, cache_dir=None,
           order="votes", country="", genre="", offset=0):
    """Browse the mirror, falling back to the pre-baked remote lists."""
    order_map = {"popular": "clicks", "trending": "votes",
                 "new": "newest", "top": "votes"}
    local = _answer_locally("", limit, min_bitrate, cache_dir,
                            order=order_map.get(section, order), country=country,
                            genre=genre, offset=offset)
    if local is not None:
        return local
    if offset:
        return []
    # The remote lists are unfiltered topclick/topvote/latest, so falling
    # back to one while a filter is set returns stations that do not match
    # what was asked for: pick a country the mirror has no stations for and
    # the list quietly fills with worldwide favourites. An empty filtered
    # result means nothing matched, and that is an answer.
    if country or genre:
        return []
    paths = {
        "popular": "/json/stations/topclick",
        "trending": "/json/stations/topvote",
        "new": "/json/stations/latestre",
    }
    path = paths.get(section, paths["popular"])
    raw, _age = cached("radio:%s" % section, lambda: _mirror_get(
        path, limit=min(500, limit * 3), hidebroken="true"), ttl=900)
    out = []
    for entry in raw if isinstance(raw, list) else []:
        if isinstance(entry, dict):
            station = _station(entry, cache_dir, min_bitrate)
            if station:
                out.append(station)
        if len(out) >= limit:
            break
    return out


def by_tag(tag, limit=50, min_bitrate=0, cache_dir=None, country=""):
    """Stations carrying a genre.

    This used to pass the genre as a free-text search term, which matched
    station *names* as well as tags - picking "pop" showed every station with
    "pop" in its title whether or not it was tagged pop at all. It is an exact
    tag filter now, and falls back to the directory's own bytag endpoint only
    when the mirror cannot answer.
    """
    tag = textutil.text(tag, 60)
    if _CATALOGUE is not None:
        try:
            rows = _CATALOGUE.search_radio(limit=limit,
                                           min_bitrate=min_bitrate,
                                           order="votes", group=tag or None,
                                           country=country or None)
        except Exception:
            rows = []
        if rows:
            out = []
            for row in rows:
                built = _from_row(row, cache_dir)
                if built:
                    out.append(built)
            if out:
                return out
    # Same reasoning as browse(): the bytag endpoint cannot filter by
    # country, so falling back to it under a country filter answers a
    # different question than the one that was asked.
    if country or _mirrored():
        return []
    raw, _age = cached("radio:tag:%s" % textutil.slug(tag, 40), lambda:
                       _mirror_get("/json/stations/bytag", tag=tag,
                                   limit=min(500, limit * 3),
                                   hidebroken="true", order="votes",
                                   reverse="true"), ttl=900)
    out = []
    for entry in raw if isinstance(raw, list) else []:
        if isinstance(entry, dict):
            station = _station(entry, cache_dir, min_bitrate)
            if station:
                out.append(station)
        if len(out) >= limit:
            break
    return out


def curated(cache_dir=None, min_bitrate=0):
    out = []
    for name, url, genre, logo in CURATED:
        if min_bitrate and min_bitrate > 128:
            continue
        out.append(item(
            "radio", "curated-%s" % textutil.slug(name, 60), name, url=url,
            kind="station", artist=genre, is_live=True,
            bitrate=128, codec="MP3",
            art=art_for(logo, "radio-%s" % name, FETCHER[0]),
            artKey="radio-%s" % name,
            extra={"genre": genre, "tags": genre, "country": "", "votes": 0,
                   "homepage": "", "codec": "MP3"},
        ))
    return out


def genres(cache_dir=None, limit=60, min_count=8):
    """[(genre, station count)], biggest first, from the facet table.

    The old version grouped by the whole comma-separated tag string, so a
    station tagged "catholic,christian,religion" was counted as one anonymous
    entry and the individual genres never appeared at all. Counting the
    per-tag rows instead is what makes this a list of genres with real
    numbers behind them.
    """
    if _CATALOGUE is not None:
        try:
            rows = _CATALOGUE.radio_genres(limit=limit, min_count=min_count)
        except Exception:
            rows = []
        if rows:
            return [(textutil.text(name, 40), int(count))
                    for name, count in rows if taxonomy.is_genre(name)]
        # Tags live in the same rows as the stations, so once those are
        # mirrored the mirror knows the genres too. Fetching a second genre
        # list from the network could disagree with it about what exists.
        if _mirrored():
            return []
    """The most-used genre tags, largest first.

    Radio Browser's /json/tags returns {name, stationcount}. Filtering on the
    count keeps the list to tags people actually use, because the raw list is
    a thousand entries of mostly-one-station noise.
    """
    raw, _age = cached("radio:genres", lambda: _mirror_get("/json/tags"),
                       ttl=6 * 3600)
    rows = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        name = textutil.text(entry.get("name"), 40)
        if not name:
            continue
        try:
            count = int(entry.get("stationcount") or 0)
        except (TypeError, ValueError):
            count = 0
        rows.append((count, name))
    rows.sort(key=lambda pair: (-pair[0], pair[1]))
    return [(name, count) for count, name in rows[:limit]
            if count > 0 and taxonomy.is_genre(name)]


def countries(cache_dir=None, limit=300):
    """[(code, name, station count)], biggest first, from the mirror.

    242 countries is far too many to be chips, which is why this is a
    dropdown; the counts are there so the list can be ordered by how much is
    actually behind each name rather than alphabetically.
    """
    if _CATALOGUE is not None:
        try:
            rows = _CATALOGUE.radio_countries(limit=limit)
        except Exception:
            rows = []
        out = []
        for code, name, count in rows:
            code = textutil.text(code, 4).upper()
            if not code:
                continue
            # The mirror carries the UN's long-form names, which do not fit a
            # dropdown next to a two-letter code.
            name = taxonomy.country_name(code, name)
            out.append((code, name, int(count)))
        if out:
            return out
    return []


# -- play reporting -------------------------------------------------------

_client_id = None


def _client_uuid(state):
    global _client_id
    if _client_id:
        return _client_id
    stored = (state.get("radio") or {}).get("client_id")
    if stored:
        _client_id = str(stored)
        return _client_id
    _client_id = str(uuid.uuid4())
    radio = dict(state.get("radio") or {})
    radio["client_id"] = _client_id
    state["radio"] = radio
    return _client_id


def report_play(station_id, state, enabled=True, callback=None):
    """Tell Radio Browser a stream was started. This is what keeps stations
    ranked, and it is the only thing we ever send anywhere.

    One anonymous UUID, generated once, no account, nothing about the machine.
    Turning the setting off makes this a no-op.
    """
    if not enabled or netguard.offline():
        return False
    station_id = textutil.text(station_id, 100)
    if not station_id:
        return False
    throttle()
    try:
        netguard.fetch(
            api_base() + "/json/url/%s" % station_id,
            limit=4096, seconds=10,
            opener=netguard._build_opener(netguard.API_PORTS, True)[0],
            allowed=netguard.API_PORTS)
    except netguard.Blocked:
        return False
    except OSError:
        return False
    # The GET above is the report; nothing else is transmitted. The uuid is
    # only sent if the mirror requires it, and we keep it local otherwise.
    if callback:
        callback()
    return True


def save_favourites(store, station_id, name, url, on=True):
    data = store.state.setdefault("radio", {}).setdefault("favourites", [])
    station_id = textutil.text(station_id, 100)
    data[:] = [f for f in data if not isinstance(f, dict) or
               f.get("uid") != station_id]
    if on:
        data.append({"uid": station_id, "title": textutil.text(name, 200),
                     "url": textutil.text(url, 2048), "at": int(time.time())})
    del data[:-500]
    store.save()
    return data


def favourites(store):
    return [f for f in (store.state.get("radio") or {}).get("favourites", [])
            if isinstance(f, dict) and f.get("url")]
