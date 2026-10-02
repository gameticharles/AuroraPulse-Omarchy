"""Reading Radio Browser's own health flag.

The directory publishes a "this stream answered" flag per station and offers a
broken-station count in its stats. Both are only useful if the value is read
correctly, and the obvious way to read it is wrong in a way that never fails
loudly: the API sends the integer 0, and in Python `0 is False` evaluates to
False, so an identity test silently treats every broken station as healthy.
That made the "hide broken stations" filter a no-op, and the 7,001 streams
upstream has given up on were all listed as playable.
"""

DEFAULT_OK = 1


def checked_flag(value, default=DEFAULT_OK):
    """Interpret Radio Browser's health flag as 1 (ok) or 0 (broken).

    `default` covers a payload that carries no opinion at all. The incremental
    endpoint omits the field entirely, and for that a station is better treated
    as unverified-but-listed than as verified-good.
    """
    if value is None:
        return default
    if isinstance(value, str):
        return 0 if value.strip().lower() in ("", "0", "false", "no") else 1
    if isinstance(value, bool):
        return 1 if value else 0
    return 1 if value else 0
