"""Repair rules for station URLs that are listed but not playable.

A URL can be well formed, present in the mirror, and still not be a stream.
These are the shapes that showed up when the mirror was *played* rather than
merely parsed, each fixed by a rule rather than by hand so the next sync gets
the repair for free.

Kept deliberately small and specific. A repair rule that guesses is worse
than no rule: it turns a station that visibly fails into one that silently
plays the wrong thing.
"""

from urllib.parse import urlsplit, urlunsplit


def repair(url):
    """Return a playable form of `url`, or None if it needs no repair.

    None means "no rule matched", so the caller can tell that apart from a
    repair that happened to produce the input unchanged.
    """
    if not url or not isinstance(url, str):
        return None
    text = url.strip()
    if not text.startswith(("http://", "https://")):
        return None

    parts = urlsplit(text)
    channel = parts.path.strip("/")
    if parts.netloc.lower() in ("laut.fm", "www.laut.fm"):
        # laut.fm answers a bare channel page with a 200 and an HTML
        # document, which mpv reports as "Failed to recognize file format" -
        # indistinguishable from a dead station to anybody browsing the list.
        # The stream is on a separate host, and every one of these played
        # after the rewrite. A channel with an extension is already a stream
        # URL and is left alone.
        if channel and "/" not in channel and "." not in channel:
            return urlunsplit(("https", "stream.laut.fm", "/" + channel,
                               parts.query, parts.fragment))
    return None


def candidates(url, alternates=()):
    """Every form worth trying for a station, best guess first.

    The listed stream comes first because that is what the mirror advertises
    and what a user recognises; each repair follows immediately after the URL
    it repairs, so a station whose only listed URL is a web page still gets a
    real attempt without waiting through the whole list.
    """
    ordered = []
    for candidate in (url,) + tuple(alternates or ()):
        if not candidate or not isinstance(candidate, str):
            continue
        candidate = candidate.strip()
        for form in (candidate, repair(candidate)):
            if form and form not in ordered:
                ordered.append(form)
    return ordered
