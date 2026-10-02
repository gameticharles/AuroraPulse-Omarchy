"""Live TV channels and the programme guide.

Playlists come from the iptv-org project, which publishes both a country
listing and a global one, with an XMLTV guide alongside. Both are a few
megabytes, so they are downloaded on a schedule into the cache and re-used,
never fetched while the user waits.
"""

import gzip
import io
import os
import re
import time

from ..util import netguard, taxonomy, textutil
from .base import (SourceError, _fetcher, art_for, cached, clean_url, fetch,
                   item, parse_m3u)

CHANNEL_LIST = "https://iptv-org.github.io/iptv/index.category.m3u"
# The language index exists purely for one field: the channel list says
# nothing about a channel's language, so the `language` column was written
# as an empty string forever. This is the same catalogue indexed by language,
# so a channel's group-title here is its language, exactly.
LANGUAGE_LIST = "https://iptv-org.github.io/iptv/index.language.m3u"
# The old single-file guide moved and now answers 404. epgshare publishes one
# file per country instead, a few megabytes each, which is also the right
# shape: a guide for the channels being looked at, not for the whole world.
EPG_URL = ""
EPG_BASE = "https://epgshare01.online/epgshare01/"
EPG_ALIASES = {"GB": "UK"}

PLAYLIST_TTL = 6 * 3600
EPG_TTL = 12 * 3600

MAX_PLAYLIST_BYTES = 48 << 20
MAX_EPG_BYTES = 96 << 20
MAX_EPG_CHANNELS = 4000
MAX_EPG_PROGRAMMES = 40000

_CATALOGUE = None


def set_catalogue(catalogue):
    global _CATALOGUE
    _CATALOGUE = catalogue


def _from_row(row):
    name = row["name"]
    # Keyed by the channel's id, not its name: names repeat across regions
    # ("Cinema", "News 24"), and two rows sharing a uid both lit up as "now
    # playing" and could not be told apart by the queue.
    ident = row["id"] or name
    key = "tv-%s" % textutil.slug(ident, 100)
    return {
        "uid": "tv:%s" % ident,
        "source": "tv",
        "kind": "channel",
        "title": name,
        "url": row["url"],
        "artist": row["group_title"] or "",
        "album": "",
        # Logos go through the same background fetcher as station art. Handing
        # the panel a remote URL meant no logo ever showed, because the panel
        # only ever loads local, verified files.
        "art": art_for(row["logo"] or "", key, _fetcher()),
        "duration": 0,
        "is_live": True,
        "codec": "",
        "bitrate": 0,
        "extra": {"group": row["group_title"] or "",
                  "attrs": "", "id": row["id"], "country": row["country"] or ""},
    }


def _mirrored():
    """Has the full channel list been downloaded and kept?

    Same rule as radio: once it has, this tab answers from disk and nothing
    else, so the rows under the cursor do not change underneath the user.
    """
    if _CATALOGUE is None:
        return False
    try:
        return bool(_CATALOGUE.mirrored("tv"))
    except Exception:
        return False


def _from_mirror(term, limit, country, group, hide_nsfw=True, offset=0,
                 hide_geo=False, hide_not247=False):
    """Answer from the mirror, or None so the caller uses the playlist.

    Returns [] rather than None once the mirror is complete: "no channel
    matched" is an answer, and it is the only answer available when the
    playlist is no longer the source.
    """
    if _CATALOGUE is None:
        return None
    try:
        rows = _CATALOGUE.search_channels(term=term, limit=limit, offset=offset,
                                          hide_geo=hide_geo,
                                          hide_not247=hide_not247,
                                          country=country, group=group,
                                          hide_nsfw=hide_nsfw)
    except Exception:
        return None
    if not rows:
        return [] if (_mirrored() or offset) else None
    return [_from_row(row) for row in rows]


_TIME = re.compile(r"(\d{14})\s*\+\s*(\d{4})")
_XML_ID = re.compile(r'channel id="([^"]+)"')
_XML_NAME = re.compile(r"<display-name>([^<]*)</display-name>")
_XML_TITLE = re.compile(r"<title[^>]*>([^<]*)</title>")
_XML_DESC = re.compile(r"<desc[^>]*>([^<]*)</desc>")
_XML_STOP = re.compile(r'<stop[^>]*\sstop="(\d{14})')


def _cache_path(name):
    return os.path.join(_cache_dir, name)


_cache_dir = None


def configure(cache_dir):
    global _cache_dir
    _cache_dir = cache_dir


def channels(country="", limit=800, cache_dir=None, group="", query="",
              hide_nsfw=True, offset=0, hide_geo=False, hide_not247=False):
    """The channel list, from the mirror when we have one."""
    if cache_dir:
        configure(cache_dir)
    # iptv-org files the United Kingdom as "UK"; a user who types GB, or who
    # picks "United Kingdom" out of the dropdown, means the same place.
    local = _from_mirror(query or "", min(int(limit), 2000),
                         taxonomy.country_code(country), group, hide_nsfw,
                         offset=offset, hide_geo=hide_geo,
                         hide_not247=hide_not247)
    if local is not None:
        return local
    if offset:
        return []
    body = _playlist_text()
    if not body:
        return []

    wanted_group = textutil.text(group, 80)
    wanted_query = textutil.text(query, 80).lower()
    wanted_country = textutil.text(country, 4).upper()

    out = []
    seen = set()
    for entry in parse_m3u(body, "tv", id_prefix="ch-", limit=100000):
        attrs = entry.get("extra", {})
        attrs_text = attrs.get("attrs", "")
        if wanted_country and wanted_country not in attrs_text.upper():
            continue
        if wanted_group and wanted_group.lower() not in \
                (attrs.get("group") or "").lower():
            continue
        if wanted_query and wanted_query not in entry["title"].lower():
            continue
        if entry["url"] in seen:
            continue
        seen.add(entry["url"])
        out.append(entry)
        if len(out) >= limit:
            break
    return out


def _playlist_text():
    """The playlist, downloaded at most every PLAYLIST_TTL."""
    path = _cache_path("iptv.m3u") if _cache_dir else None
    if path and os.path.exists(path):
        age = time.time() - os.path.getmtime(path)
        if age < PLAYLIST_TTL:
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    return f.read()
            except OSError:
                pass
    if netguard.offline():
        raise SourceError("offline mode is on and the channel list is not "
                          "cached yet", "denied")
    try:
        body = fetch(CHANNEL_LIST, limit=MAX_PLAYLIST_BYTES, seconds=90,
                     allowed=netguard.WEB_PORTS).decode("utf-8", "replace")
    except SourceError:
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()      # a stale list beats no list
        raise
    if path:
        _write(path, body.encode("utf-8"))
    return body


def _write(path, data):
    tmp = path + ".part"
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def countries():
    """[(code, label, count)] for the filter row.

    This returned bare codes, so the dropdown read "UK", "DO", "CZ" and 175
    others with nothing to look up. The label is a plain lookup - the codes
    themselves are untouched, because they are what the mirror stores and
    what a filter has to match.
    """
    if _CATALOGUE is not None:
        try:
            found = _CATALOGUE.channel_countries()
            if found:
                return [(code, taxonomy.country_label(code), int(n))
                        for code, n in found]
        except Exception:
            pass
        if _mirrored():
            return []
    body = _playlist_text()
    found = set()
    # iptv-org's own index is the reliable source for the country codes; the
    # playlist only carries them inside #EXTINF attribute soup.
    try:
        data = _index_json()
        for entry in data if isinstance(data, list) else []:
            code = textutil.text(entry.get("country_code"), 4)
            if code:
                found.add(code.upper())
    except SourceError:
        pass
    return sorted(found)


def _index_json():
    from .base import fetch_json
    return fetch_json("https://iptv-org.github.io/api/channels.json",
                      limit=8 << 20, seconds=30, allowed=netguard.WEB_PORTS)


def channel_groups():
    """[(category, channel count)] from the facet table.

    The old version counted group-title on the channel rows, which only ever
    held the *first* category of each channel, so "sports" looked empty for
    every channel filed under "news, sports". The facet table has every
    category each channel belongs to.
    """
    if _CATALOGUE is not None:
        try:
            genres = _CATALOGUE.channel_genres(limit=60, min_count=4)
            if genres:
                return genres
        except Exception:
            pass
        try:
            groups = _CATALOGUE.channel_groups()
            if groups:
                return groups
        except Exception:
            pass
        if _mirrored():
            return []
    body = _playlist_text()
    found = {}
    for line in body.splitlines():
        if not line.startswith("#EXTINF"):
            continue
        match = re.search(r'group-title="([^"]*)"', line)
        if match:
            name = textutil.text(match.group(1), 60)
            if name:
                found[name] = found.get(name, 0) + 1
    return sorted(found.items(), key=lambda pair: -pair[1])[:60]


# -- guide ----------------------------------------------------------------

def guide(cache_dir, url=EPG_URL, limit_channels=MAX_EPG_CHANNELS):
    """The programme guide, parsed into {channel: [programmes]}.

    XMLTV is parsed with a regex rather than a real parser on purpose: the
    file is tens of megabytes of trusted public data, and the alternative is
    building a tree we would immediately throw away. Nothing here ever writes
    to disk, so a hostile entity cannot expand.
    """
    if cache_dir:
        configure(cache_dir)
    path = _local("epg.json")
    if path and os.path.exists(path):
        if time.time() - os.path.getmtime(path) < EPG_TTL:
            from ..core.state import read_json
            cached_guide = read_json(path, None)
            if isinstance(cached_guide, dict):
                return cached_guide

    if netguard.offline():
        raise SourceError("offline mode is on and the guide is not cached",
                          "denied")
    try:
        raw = fetch(url or EPG_URL, limit=MAX_EPG_BYTES, seconds=180,
                    allowed=netguard.WEB_PORTS)
    except SourceError:
        from ..core.state import read_json
        stale = read_json(path, None) if path else None
        if isinstance(stale, dict) and stale:
            return stale
        raise

    try:
        text = gzip.decompress(raw).decode("utf-8", "replace")
    except (OSError, EOFError):
        text = raw.decode("utf-8", "replace")

    guide_map = _parse_xmltv(text, limit_channels)
    if guide_map and path:
        from ..core.state import write_json
        try:
            write_json(path, guide_map)
        except OSError:
            pass
    return guide_map


def _parse_xmltv(text, limit_channels):
    result = {}
    channel_ids = set()
    for name in _XML_ID.findall(text)[:limit_channels]:
        channel_ids.add(name)
    if not channel_ids:
        return result

    total = 0
    for block in re.findall(r"<programme\b.*?</programme>", text,
                            re.DOTALL)[:MAX_EPG_PROGRAMMES]:
        if total >= MAX_EPG_PROGRAMMES:
            break
        start = _TIME.search(block)
        stop = _XML_STOP.search(block)
        if not start or not stop:
            continue
        channel = re.search(r'channel="([^"]+)"', block)
        if not channel or channel.group(1) not in channel_ids:
            continue
        title = _XML_TITLE.search(block)
        desc = _XML_DESC.search(block)
        if not title:
            continue
        start_at = _to_epoch(start.group(1), start.group(2))
        stop_at = _to_epoch(stop.group(1), stop.group(2))
        if not start_at or not stop_at or stop_at <= start_at:
            continue
        if stop_at - start_at > 12 * 3600:
            continue          # a placeholder block, not a programme
        result.setdefault(channel.group(1), []).append({
            "start": start_at,
            "stop": stop_at,
            "title": textutil.text(_unescape(title.group(1)), 200),
            "desc": textutil.text(_unescape(desc.group(1)), 400) if desc else "",
        })
        total += 1
    for programmes in result.values():
        programmes.sort(key=lambda p: p["start"])
        del programmes[400:]
    return result


def _to_epoch(stamp, offset):
    """XMLTV timestamps are local wall time plus a zone offset."""
    try:
        from datetime import datetime, timedelta, timezone
        naive = datetime.strptime(stamp, "%Y%m%d%H%M%S")
        sign = 1 if offset[0] != "-" else -1
        delta = timedelta(hours=int(offset[1:3]), minutes=int(offset[3:5]))
        return int((naive - sign * delta).replace(
            tzinfo=timezone.utc).timestamp())
    except (ValueError, IndexError):
        return 0


def _unescape(value):
    import html
    return html.unescape(value or "")


def now_playing(guide_map, channel_name, at=None):
    """Find the programme on right now for a channel."""
    if not channel_name or not guide_map:
        return None
    at = int(at if at is not None else time.time())
    candidates = [k for k in guide_map if k.lower() == channel_name.lower()]
    if not candidates:
        needle = channel_name.lower()
        candidates = [k for k in guide_map if needle in k.lower()][:4]
    for key in candidates:
        for programme in guide_map.get(key, []):
            if programme["start"] <= at < programme["stop"]:
                return programme
    return None


def upcoming(guide_map, channel_name, count=8, at=None):
    if not channel_name or not guide_map:
        return []
    at = int(at if at is not None else time.time())
    needle = channel_name.lower()
    out = []
    for key, programmes in guide_map.items():
        if key.lower() != needle and needle not in key.lower():
            continue
        out.extend(p for p in programmes if p["start"] >= at)
    out.sort(key=lambda p: p["start"])
    return out[:count]


# -- per-country guides ------------------------------------------------------

_GUIDE_LOCK = __import__("threading").Lock()
_GUIDES = {}            # file code -> (loaded_at, guide)


def _epg_index():
    """The guide files epgshare publishes, e.g. {"UK1", "DE1", "US2"}."""
    def load():
        body = fetch(EPG_BASE, limit=2 << 20, seconds=20,
                     allowed=netguard.WEB_PORTS).decode("utf-8", "replace")
        return sorted(set(re.findall(r"epg_ripper_([A-Z]{2}\d)\.xml\.gz", body)))
    codes, _age = cached("tv:epg-index", load, ttl=24 * 3600)
    return codes


def epg_file_for(country):
    code = EPG_ALIASES.get((country or "").upper(), (country or "").upper())
    if len(code) != 2:
        return ""
    try:
        files = [f for f in _epg_index() if f.startswith(code)]
    except SourceError:
        files = [code + "1"]
    return sorted(files)[0] if files else ""


def _norm(value):
    text = (value or "").lower()
    text = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", text)       # (1080p) [Geo-blocked]
    text = re.sub(r"\b(hd|sd|fhd|uhd|4k|hevc)\b", " ", text)
    return re.sub(r"[^a-z0-9+]", "", text)


def _parse_guide(text, window_start, window_end):
    channels = {}
    for cid, body in re.findall(r'<channel id="([^"]+)"[^>]*>(.*?)</channel>', text, re.S):
        names = re.findall(r"<display-name[^>]*>([^<]*)", body)
        channels[cid] = _unescape(names[0]) if names else cid
    programmes = {}
    for block in re.finditer(r"<programme\b(.*?)>(.*?)</programme>", text, re.S):
        head, inner = block.group(1), block.group(2)
        start = re.search(r'start="(\d{14})\s*([+-]\d{4})?', head)
        stop = re.search(r'stop="(\d{14})\s*([+-]\d{4})?', head)
        channel = re.search(r'channel="([^"]+)"', head)
        if not (start and stop and channel):
            continue
        start_at = _to_epoch(start.group(1), (start.group(2) or "+0000").lstrip("+"))
        stop_at = _to_epoch(stop.group(1), (stop.group(2) or "+0000").lstrip("+"))
        if not start_at or not stop_at or stop_at <= window_start or start_at >= window_end:
            continue
        title = _XML_TITLE.search(inner)
        desc = _XML_DESC.search(inner)
        programmes.setdefault(channel.group(1), []).append({
            "start": start_at, "stop": stop_at,
            "title": textutil.text(_unescape(title.group(1)) if title else "", 200),
            "desc": textutil.text(_unescape(desc.group(1)), 400) if desc else "",
        })
    for rows in programmes.values():
        rows.sort(key=lambda p: p["start"])
    index = {}
    for cid, name in channels.items():
        for key in (_norm(name), _norm(re.sub(r"\.[a-z]{2}$", "", cid).replace(".", " "))):
            if key and key not in index:
                index[key] = cid
    return {"channels": channels, "programmes": programmes, "index": index}


def country_guide(country, cache_dir=None, max_age=12 * 3600, custom_url=""):
    """The guide for one country's channels, cached on disk and in memory."""
    url = custom_url or ""
    code = "custom" if url else epg_file_for(country)
    if not code:
        return None
    if not url:
        url = EPG_BASE + "epg_ripper_%s.xml.gz" % code
    now = time.time()
    with _GUIDE_LOCK:
        hit = _GUIDES.get(code)
        if hit and now - hit[0] < max_age:
            return hit[1]
    path = os.path.join(cache_dir, "epg-%s.json" % code) if cache_dir else None
    from ..core.state import read_json, write_json
    if path and os.path.exists(path) and now - os.path.getmtime(path) < max_age:
        guide = read_json(path, None)
        if isinstance(guide, dict) and guide.get("programmes") is not None:
            with _GUIDE_LOCK:
                _GUIDES[code] = (os.path.getmtime(path), guide)
            return guide
    if netguard.offline():
        return None
    raw = fetch(url, limit=MAX_EPG_BYTES, seconds=120, allowed=netguard.WEB_PORTS)
    try:
        text = gzip.decompress(raw).decode("utf-8", "replace")
    except (OSError, EOFError):
        text = raw.decode("utf-8", "replace")
    guide = _parse_guide(text, now - 6 * 3600, now + 36 * 3600)
    with _GUIDE_LOCK:
        _GUIDES[code] = (now, guide)
    if path:
        try:
            write_json(path, guide)
        except OSError:
            pass
    return guide


def match_channel(guide, title, ident=""):
    """The guide's id for one of our channels, or "" if it has none."""
    if not guide:
        return ""
    index = guide.get("index") or {}
    key = _norm(title)
    if key in index:
        return index[key]
    base = _norm((ident or "").split("@")[0].split(".")[0])
    if base and base in index:
        return index[base]
    if len(key) >= 4:
        found = [k for k in index if k.startswith(key)]
        if found:
            return index[min(found, key=len)]
    return ""


def schedule(channel, start, end, cache_dir=None, max_age=12 * 3600, custom_url=""):
    """Programmes for one channel item between start and end (epoch seconds)."""
    extra = channel.get("extra") or {}
    country = extra.get("country") or channel.get("country") or ""
    try:
        guide = country_guide(country, cache_dir, max_age, custom_url)
    except SourceError:
        guide = None
    cid = match_channel(guide, channel.get("title", ""), extra.get("id") or channel.get("id", ""))
    if not cid:
        return []
    return [p for p in (guide.get("programmes") or {}).get(cid, [])
            if p["stop"] > start and p["start"] < end]
