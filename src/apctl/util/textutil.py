"""Text sanitisation.

Every string that reaches the shell from a remote source passes through here
first. The QML side renders everything as Text.PlainText; this is the other
half of that guarantee, because a bidi override or a control character is a
rendering attack even in plain text.

Three separate problems are solved here, and they need separate passes:

* markup, because a station that calls itself ``<b>BBC</b> One`` should render
  as text, not as something the compositor tries to interpret
* invisible characters, which hide text and reorder it
* size, because a directory is free to return a 4 KB title
"""

import re
import unicodedata

# Zero-width and format characters: the bidi overrides U+202A-U+202E and the
# isolates U+2066-U+2069 live in here, and both can make text read in an order
# that is not the order it is stored in. Dropped entirely.
_DROP = re.compile(
    "[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u206f\ufeff]"
)
# Whitespace that is not a newline, including the exotic Unicode spaces that
# a title generator will happily use to defeat a naive split.
_SPACE = re.compile(
    r"[\s\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+"
)
# Angle-bracket markup, including the self-closing and unterminated forms.
_TAG = re.compile(r"<[^>]{0,400}>|<[^<]{0,400}?$")
# Comment and doctype prologues, which are not tags but read like them.
_PROLOGUE = re.compile(r"<!--.*?-->|<!\[CDATA\[.*?\]\]>|<!\w[^>]*>",
                       re.DOTALL)

ELLIPSIS = "…"


def _to_text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    # A dict or list has no business becoming a string a user reads.
    return ""


def _scrub(value):
    """Strip markup and invisible characters, keeping line structure."""
    text = _to_text(value)
    if not text:
        return ""
    # Surrogates survive surrogatepass but break a strict-mode JSON write, so
    # they are resolved here rather than caught at the point of writing.
    text = text.encode("utf-8", "surrogatepass").decode("utf-8", "replace")
    text = _PROLOGUE.sub(" ", text)
    text = _TAG.sub(" ", text)
    text = _DROP.sub("", text)
    # Everything in the Unicode "control" and "format" categories except the
    # newline becomes a space; deleting them would weld words together.
    out = []
    for ch in text:
        if ch == "\n":
            out.append("\n")
            continue
        category = unicodedata.category(ch)
        if category[0] in "CZ":
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def _truncate(value, limit):
    """Cut to limit, marking that we did.

    The marker matters: a title that silently loses its last three words
    looks like a different title rather than a truncated one.
    """
    if not limit or len(value) <= limit:
        return value
    return value[:max(0, limit - 1)].rstrip() + ELLIPSIS


def text(value, limit=256):
    """Single-line safe text: no markup, no newlines, whitespace collapsed."""
    scrubbed = _SPACE.sub(" ", _scrub(value).replace("\n", " ")).strip()
    return _truncate(scrubbed, limit)


def multiline(value, limit=2000, lines=40):
    """Text that keeps its line structure, for descriptions and lyric lines.

    Both caps are real: a lyric file from the internet is a hostile input
    until proven otherwise, and 40 lines is more than any UI here shows.
    """
    scrubbed = _scrub(value)
    out = []
    total = 0
    for raw in scrubbed.split("\n"):
        line = _SPACE.sub(" ", raw).strip()
        if not line:
            continue
        if len(out) >= lines:
            break
        room = limit - total
        if room <= 1:
            break
        if len(line) > room:
            line = _truncate(line, room)
        out.append(line)
        total += len(line) + 1
    return "\n".join(out)


def slug(value, limit=64):
    """A filename-safe token, used for uid-derived file names."""
    cleaned = re.sub(r"[^a-z0-9]+", "-", text(value, limit).lower())
    return cleaned.strip("-")[:limit] or "item"


def int_in(value, low, high, fallback=0):
    """Clamp an integer that came from a remote source or a settings file."""
    try:
        # int() of a float truncates, which is what a drag handler wants; int()
        # of a bool is its numeric value, which is not.
        if isinstance(value, bool):
            raise ValueError
        number = int(value)
    except (TypeError, ValueError):
        try:
            number = int(float(value))
        except (TypeError, ValueError):
            return fallback
    if number < low:
        return low
    if number > high:
        return high
    return number


def bool_of(value, fallback=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return fallback
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def initials(name, fallback="AP"):
    """"Radio Paradise" -> "RP". Used for artwork placeholders."""
    words = [w for w in re.split(r"[\s._-]+", text(name, 120)) if w]
    if not words:
        return fallback
    if len(words) == 1:
        return words[0][:2].upper()
    return (words[0][0] + words[1][0]).upper()


def duration(seconds):
    """Seconds -> m:ss, or h:mm:ss past an hour. Empty for a live stream."""
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return ""
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def split_title(title):
    """"Artist - Song" -> (artist, song). A title with no separator is a song."""
    cleaned = re.sub(r"\s+", " ", _to_text(title).strip())
    match = re.match(r"^(.+?)\s+[-\u2013\u2014]\s+(.+)$", cleaned)
    if not match:
        return "", text(cleaned, 512)
    return text(match.group(1), 256), text(match.group(2), 512)
