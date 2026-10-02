"""Unit tests for the parts where a bug is silent rather than loud.

Run with:  python3 tests/test_units.py
or:        ./ap-ctl selftest
"""

import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from apctl.core import legacy, protocol, repair, resolver, state  # noqa: E402
from apctl.core.resolver import parse_lrc  # noqa: E402
from apctl.sources import base  # noqa: E402
from apctl.util import (health, netguard, sandbox, taxonomy,  # noqa: E402
                       textutil)


class Text(unittest.TestCase):
    def test_strips_markup_and_unicode_tricks(self):
        for raw, bad in (("<b>x</b>", "<"), ("a\u202eb", "\u202e"),
                         ("\x1b[31mred\x1b[0m", "\x1b"),
                         ("<script>alert(1)</script>", "<")):
            self.assertNotIn(bad, textutil.text(raw, 100), raw)

    def test_collapses_and_trims(self):
        self.assertEqual(textutil.text("  hello   world \n ", 100), "hello world")

    def test_truncates_with_marker(self):
        self.assertTrue(textutil.text("x" * 500, 50).endswith("…"))
        self.assertLessEqual(len(textutil.text("x" * 500, 50)), 51)

    def test_rejects_nonsense_scalars(self):
        self.assertEqual(textutil.int_in("nope", 0, 10, 7), 7)
        self.assertEqual(textutil.int_in(1e9, 0, 10, 7), 10)
        self.assertEqual(textutil.int_in(-5, 0, 10, 7), 0)
        self.assertEqual(textutil.int_in("5", 0, 10, 7), 5)
        self.assertIs(textutil.bool_of("yes"), True)
        self.assertIs(textutil.bool_of(0), False)

    def test_multiline_cap(self):
        out = textutil.multiline("a\n" * 500, 100, 10)
        self.assertLessEqual(len(out.split("\n")), 10, "line cap")
        self.assertLessEqual(len(out), 100, "length cap")


class LazyYouTubePathsResolve(unittest.TestCase):
    """The YouTube paths that are imported inside a function.

    Two bugs here were this shape and neither showed up in any test:

      * `from . import base` inside `resolver.search` - `apctl.core.base` has
        never existed, `base` lives in `apctl.sources`. YouTube search failed
        for everyone, always, with "cannot import name 'base'".
      * `netguard.url_host(...)` in `is_youtube_url` - no such function was
        ever written. Search worked and playback died instead, with "no
        attribute 'url_host'", which is a strange way to learn that.

    Neither was visible because a module import does not resolve the names
    used inside its functions, and both were reachable only by hitting the
    network. Calling them is the only honest check, so the network is stubbed.
    """

    def test_is_youtube_url_recognises_the_hosts(self):
        for url in ("https://www.youtube.com/watch?v=abc",
                    "https://youtu.be/abc", "https://m.youtube.com/watch?v=abc",
                    "https://music.youtube.com/watch?v=abc"):
            self.assertTrue(resolver.is_youtube_url(url), url)
        for url in ("https://example.com/watch?v=abc", "not a url", "",
                    "https://youtube.com.evil.test/watch?v=abc"):
            self.assertFalse(resolver.is_youtube_url(url), url)

    def test_url_host_strips_the_parts_that_are_not_the_host(self):
        for url, want in (("https://WWW.YouTube.com/watch?v=a", "www.youtube.com"),
                          ("https://user:pw@example.com:8443/x", "example.com"),
                          ("https://example.com./x", "example.com"),
                          ("http://[::1]/x", "::1"),
                          ("nonsense", ""), ("", "")):
            self.assertEqual(netguard.url_host(url), want, url)

    def test_search_does_not_import_a_module_that_does_not_exist(self):
        """The import used to sit inside the function, so it only failed live."""
        from apctl.core import resolver as res
        calls = []

        def fake(argv, timeout=None, offline_ok=False, listing=False):
            calls.append((list(argv), listing))
            return {"_type": "playlist", "entries": [
                {"id": "abc123", "title": "One", "duration": 100}]}

        original = res._ytdlp
        res._ytdlp = fake
        self.addCleanup(setattr, res, "_ytdlp", original)

        raw = res.search("a query", limit=10)
        self.assertEqual(len(raw.get("entries") or []), 1)
        self.assertTrue(calls[0][1], "a search is a listing, so it must ask "
                        "yt-dlp for a listing")

    def test_a_search_asks_yt_dlp_for_a_playlist_not_one_video(self):
        """--no-playlist on a search collapses the results to the first.

        That is why a search used to return a single result and look broken:
        the flag meant for resolving one watch URL was applied to every
        listing call as well.
        """
        from apctl.core import resolver as res
        seen = {}

        class Proc:
            returncode = 0
            stderr = ""
            stdout = '{"_type": "playlist", "entries": [{"id": "a", "url": "u"}]}'

        original_run = subprocess.run

        def fake_run(cmd, **kwargs):
            seen["cmd"] = cmd
            return Proc()

        subprocess.run = fake_run
        self.addCleanup(setattr, subprocess, "run", original_run)
        out = res._ytdlp(["--dump-single-json", "--flat-playlist", "ytsearch5:x"],
                         listing=True)
        self.assertIn("--yes-playlist", seen["cmd"])
        self.assertNotIn("--no-playlist", seen["cmd"])
        self.assertNotIn("--print-json", seen["cmd"],
                         "--print-json fights --dump-single-json and returns "
                         "entries one at a time instead of the wrapper")
        self.assertEqual(len(out.get("entries") or []), 1)

    def test_a_listing_wrapper_without_a_url_is_still_a_result(self):
        """The wrapper is a container, so having no url of its own is normal.

        Requiring one is what made every search and playlist come back empty.
        """
        from apctl.core import resolver as res
        class Proc:
            returncode = 0
            stderr = ""
            stdout = '{"_type": "playlist", "id": "q", "entries": [{"id": "a"}]}'
        original_run = subprocess.run
        subprocess.run = lambda cmd, **kw: Proc()
        self.addCleanup(setattr, subprocess, "run", original_run)
        out = res._ytdlp(["--dump-single-json", "--flat-playlist", "ytsearch5:x"],
                         listing=True)
        self.assertIsNotNone(out.get("entries"))

    def test_music_results_are_labelled_music_not_youtube(self):
        """Otherwise the bar shows the YouTube label for a Music track."""
        from apctl.sources import music
        entry = {"id": "abc123def", "title": "Pink Floyd - High Hopes",
                 "duration": 338, "uploader": "Pink Floyd"}
        built = music.to_track(entry, None, 0, "ym-test")
        self.assertIsNotNone(built)
        self.assertEqual(built.get("source"), "music")
        self.assertEqual(built.get("kind"), "track")
        self.assertEqual(built.get("artist"), "Pink Floyd")


class NetPolicy(unittest.TestCase):
    def test_rejects_everything_not_public(self):
        for address in ("127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.1",
                        "169.254.1.1", "100.64.0.1", "0.0.0.0", "224.0.0.1",
                        "::1", "fe80::1", "fc00::1", "64:ff9b::7f00:1",
                        "255.255.255.255", "::", "2002:7f00:1::"):
            self.assertFalse(netguard.public_ip(address), address)

    def test_accepts_public(self):
        for address in ("8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"):
            self.assertTrue(netguard.public_ip(address), address)

    def test_blocks_scheme_and_localhost(self):
        for url in ("file:///etc/passwd", "ftp://x/y", "gopher://x",
                    "http://127.0.0.1/", "http://localhost/", "http://[::1]/"):
            with self.assertRaises(netguard.Blocked, msg=url):
                netguard.fetch(url, seconds=3)

    def test_blocks_bad_ports(self):
        with self.assertRaises(netguard.Blocked):
            netguard.public_connection("example.com", 22, (22, 80, 443), seconds=3)
        with self.assertRaises(netguard.Blocked):
            netguard.public_connection("example.com", 11211, (11211,), seconds=3)

    def test_media_ports_allow_the_odd_ports_channels_actually_use(self):
        """A fifth of the TV catalogue streams from outside 80/443.

        8000, 1935, 8080, 8989 and friends are ordinary for IPTV, and refusing
        them meant those channels could never play at all. The port was never
        the defence that mattered - see the refusals below.
        """
        for port in (8000, 1935, 8080, 8989, 19360, 4):
            netguard._check_port(port, netguard.MEDIA_PORTS)
        # A channel on the usual ports still has to work.
        for port in (80, 443):
            netguard._check_port(port, netguard.MEDIA_PORTS)

    def test_media_ports_still_refuse_bad_ports(self):
        """Widening the policy must not widen the blocked list with it."""
        for port in (22, 23, 25, 79, 135, 137, 143, 993, 1719, 5060):
            self.assertIn(port, netguard.BAD_PORTS,
                          "test is asserting on a port that is not blocked")
            with self.assertRaises(netguard.Blocked, msg=str(port)) as caught:
                netguard._check_port(port, netguard.MEDIA_PORTS)
            self.assertIn("blocked-ports", str(caught.exception),
                          "refused for the wrong reason")

    def test_widening_ports_does_not_widen_the_address_policy(self):
        """The point of the port list was never the defence.

        If ports are open, the address checks are what stop a media URL
        reaching something on the user's own network, so they have to hold on
        an odd port exactly as they do on 80. Cloud metadata is the one that
        matters most.
        """
        for host, port in (("127.0.0.1", 8989), ("10.0.0.1", 8000),
                           ("192.168.1.1", 8080), ("169.254.169.254", 8989),
                           ("::1", 8000), ("169.254.169.254", 80)):
            with self.assertRaises(netguard.Blocked, msg="%s:%d" % (host, port)):
                netguard.public_connection(host, port, netguard.MEDIA_PORTS,
                                           seconds=3)

    def test_the_web_policy_is_unchanged(self):
        """Only playback got the wider policy; artwork and API fetching did not."""
        with self.assertRaises(netguard.Blocked):
            netguard._check_port(8080, netguard.WEB_PORTS)
        with self.assertRaises(netguard.Blocked):
            netguard._check_port(8000, netguard.API_PORTS)
        netguard._check_port(443, netguard.WEB_PORTS)

    def test_http_status_is_a_blocked_but_distinguishable(self):
        self.assertTrue(issubclass(netguard.HTTPStatus, netguard.Blocked))
        err = netguard.HTTPStatus(404, "https://x/y")
        self.assertEqual(err.status, 404)


class Seccomp(unittest.TestCase):
    def test_program_loads_and_clamps_itself(self):
        program = sandbox.seccomp_program()
        self.assertIsNotNone(program)
        self.assertEqual(len(program) % 8, 0, "BPF is 8 bytes per instruction")
        self.assertLessEqual(len(program) // 8, 4096, "kernel instruction limit")
        # Every instruction must be one the classic BPF validator accepts.
        allowed = {0x00, 0x01, 0x04, 0x05, 0x06, 0x15, 0x20, 0x25, 0x35, 0x45}
        for index in range(0, len(program), 8):
            code = program[index]
            self.assertIn(code, allowed, "bad opcode 0x%02x at %d" % (code, index // 8))

    def test_deny_list_has_no_duplicates(self):
        seen = set()
        for number in sandbox._SECCOMP_DENY:
            self.assertNotIn(number, seen, "duplicate deny entry %d" % number)
            seen.add(number)


class Assembler(unittest.TestCase):
    """The bug that started all this: BPF_JA keeps its offset in k, not jt."""

    def test_ja_offset_goes_in_k(self):
        program = sandbox.seccomp_program()
        jumps = 0
        for index in range(0, len(program), 8):
            code = program[index]
            if code == 0x05:  # BPF_JMP | BPF_JA
                jumps += 1
                k = int.from_bytes(program[index + 4:index + 8], "little")
                jt = program[index + 1]
                self.assertNotEqual(k, 0, "a JA with offset 0 is a silent fallthrough")
                # A hand-rolled assembler that put the offset in jt leaves
                # k == 0; that is the failure we are guarding against.
                self.assertEqual(jt, 0,
                                 "JA must not use jt for its offset")
        self.assertGreater(jumps, 0, "expected at least one unconditional jump")


class Protocol(unittest.TestCase):
    def test_clean_item_rejects_rubbish(self):
        self.assertIsNone(protocol.clean_item("not a dict"))
        self.assertIsNone(protocol.clean_item({}))
        self.assertIsNone(protocol.clean_item({"uid": "no-colon", "source": "radio"}))
        self.assertIsNone(protocol.clean_item({"uid": "radio:x", "source": "porn"}))

    def test_clean_item_caps_lengths(self):
        item = protocol.clean_item({
            "uid": "radio:" + "a" * 900,
            "source": "radio",
            "title": "t" * 5000,
            "artist": "a" * 5000,
            "duration": 10 ** 9,
            "url": "u" * 9000,
        })
        self.assertLessEqual(len(item["uid"]), 201)
        self.assertLessEqual(len(item["title"]), 513)
        self.assertLessEqual(item["duration"], 86400 * 7)

    def test_clean_item_keeps_only_scalars_in_extra(self):
        item = protocol.clean_item({
            "uid": "radio:x", "source": "radio", "title": "t",
            "extra": {"ok": 1, "nested": {"no": 1}, "text": "y" * 9000},
        })
        self.assertEqual(item["extra"]["ok"], 1)
        self.assertNotIn("nested", item["extra"])
        self.assertLessEqual(len(item["extra"]["text"]), 512)

    def test_read_lines_handles_text_and_binary(self):
        import io
        for stream in (io.BytesIO(b'{"cmd":"a"}\n{"cmd":"b"}\n'),
                       io.StringIO('{"cmd":"a"}\n{"cmd":"b"}\n')):
            self.assertEqual([x["cmd"] for x in protocol.read_lines(stream)],
                             ["a", "b"])

    def test_read_lines_survives_garbage(self):
        import io
        stream = io.BytesIO(b"not json\n\n[]\n{\"cmd\":\"ok\"}\n")
        self.assertEqual([x["cmd"] for x in protocol.read_lines(stream)], ["ok"])

    def test_read_lines_drops_overlong_lines(self):
        import io
        big = b'{"x":"' + b"a" * (2 << 20) + b'"}\n{"cmd":"after"}\n'
        got = [x["cmd"] for x in protocol.read_lines(io.BytesIO(big))]
        self.assertEqual(got, ["after"], "an oversized line must not desync")


class State(unittest.TestCase):
    def test_write_is_atomic_and_private(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            state.write_json(path, {"a": 1})
            self.assertEqual(state.read_json(path), {"a": 1})
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            state.write_json(path, {"a": 2})
            self.assertEqual(state.read_json(path), {"a": 2})

    def test_corrupt_file_degrades_to_fallback(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "s.json")
            with open(path, "w") as handle:
                handle.write("{not json")
            self.assertEqual(state.read_json(path, {"d": 1}), {"d": 1})

    def test_symlink_is_refused_not_followed(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "target")
            with open(target, "w") as handle:
                handle.write("original")
            link = os.path.join(tmp, "link")
            os.symlink(target, link)
            # A read through a symlink is refused rather than followed.
            self.assertIsNone(state.read_json(link, None))
            state.write_json(link, {"replaced": True})
            with open(target) as handle:
                self.assertEqual(handle.read(), "original",
                                 "a symlink must not redirect our write")

    def test_process_identity_is_stable_and_reuse_safe(self):
        self.assertEqual(state.process_start(os.getpid()),
                         state.process_start(os.getpid()))
        self.assertTrue(state.process_alive(os.getpid(),
                                            state.process_start(os.getpid())))
        self.assertFalse(state.process_alive(os.getpid(), "not-the-start-time"))


class Urls(unittest.TestCase):
    def test_clean_url_drops_tracking_only(self):
        self.assertEqual(base.clean_url("https://a.com/x?utm_source=z&k=1"),
                         "https://a.com/x?k=1")
        self.assertEqual(base.clean_url("https://a.com/x?k=1&k=2"),
                         "https://a.com/x?k=1&k=2")

    def test_clean_url_rejects_non_http(self):
        for url in ("file:///etc/passwd", "javascript:alert(1)", "",
                    "https://", "not a url"):
            self.assertEqual(base.clean_url(url), "", url)


class M3U(unittest.TestCase):
    BODY = (
        '#EXTM3U\n'
        '#EXTINF:-1 tvg-id="bbc.one" tvg-logo="https://e.com/b.png" '
        'group-title="UK",BBC One\n'
        'http://example.com/a.m3u8\n'
        '#EXTINF:-1,BBC Two\n'
        'https://example.com/b.m3u8\n'
        '#EXTINF:-1,Dup\n'
        'https://example.com/b.m3u8\n'
    )

    def test_parses_entries_and_dedupes(self):
        items = base.parse_m3u(self.BODY, "tv", id_prefix="ch-")
        self.assertEqual(len(items), 2, "the duplicate URL should be dropped")
        self.assertEqual(items[0]["title"], "BBC One")
        self.assertEqual(items[0]["art"]["url"], "https://e.com/b.png")

    def test_logo_must_be_http(self):
        body = '#EXTINF:-1 tvg-logo="file:///etc/passwd",x\nhttp://a.com/x\n'
        self.assertEqual(base.parse_m3u(body, "tv")[0]["art"]["url"], "")


class HealthFlag(unittest.TestCase):
    """Radio Browser sends the integer 0, and `0 is False` is False.

    Reading the flag with an identity test therefore reported every broken
    stream as healthy, which quietly turned the "hide broken stations" filter
    into a no-op for the whole catalogue.
    """

    def test_zero_is_broken(self):
        self.assertEqual(health.checked_flag(0), 0)
        self.assertEqual(health.checked_flag("0"), 0)
        self.assertEqual(health.checked_flag("false"), 0)
        self.assertEqual(health.checked_flag(False), 0)

    def test_healthy_values(self):
        for value in (1, 2, True, "1", "true", "anything"):
            self.assertEqual(health.checked_flag(value), 1, value)

    def test_absent_falls_back_to_the_default(self):
        # The incremental endpoint omits the field entirely.
        self.assertEqual(health.checked_flag(None), 1)
        self.assertEqual(health.checked_flag(None, 0), 0)

    def test_the_bug_this_replaces_would_have_passed_zero(self):
        self.assertIsNot(0, False, "premise: the identity test is wrong")


class UrlRepair(unittest.TestCase):
    def test_laut_page_becomes_a_stream(self):
        # A bare laut.fm channel answers 200 with an HTML page, which mpv
        # reports as an unreadable file rather than a dead station.
        self.assertEqual(repair.repair("https://laut.fm/jahfari"),
                         "https://stream.laut.fm/jahfari")
        self.assertEqual(repair.repair("https://laut.fm/country-fm24?autoplay=1"),
                         "https://stream.laut.fm/country-fm24?autoplay=1")

    def test_leaves_real_streams_alone(self):
        for url in ("https://stream.laut.fm/luca", "https://laut.fm/x.pls",
                    "http://icecast.example/live", "", None, "not a url"):
            self.assertIsNone(repair.repair(url), url)

    def test_candidate_order_and_dedupe(self):
        got = repair.candidates("https://laut.fm/jah", ["https://b/s",
                                                        "https://b/s"])
        self.assertEqual(got, ["https://laut.fm/jah",
                               "https://stream.laut.fm/jah",
                               "https://b/s"])
        self.assertEqual(len(got), len(set(got)), "no duplicate attempts")

    def test_blank_entries_are_dropped(self):
        self.assertEqual(repair.candidates("http://a/s", [None, "", "  "]),
                         ["http://a/s"])


class Taxonomy(unittest.TestCase):
    """The labels a filter list is built from have to fit in it."""

    def test_country_names_lose_the_un_long_form(self):
        self.assertEqual(
            taxonomy.country_name(
                "GB", "The United Kingdom Of Great Britain And Northern Ireland"),
            "United Kingdom")
        self.assertEqual(
            taxonomy.country_name("US", "The United States Of America"),
            "United States")
        self.assertEqual(taxonomy.country_name("DE", "Germany"), "Germany")

    def test_country_names_fix_the_clumsy_ones(self):
        self.assertEqual(taxonomy.country_name("RU", "The Russian Federation"),
                         "Russia")
        self.assertEqual(taxonomy.country_name("KR", "Korea, Republic of"),
                         "South Korea")
        self.assertEqual(
            taxonomy.country_name("BO", "Bolivia, Plurinational State Of"),
            "Bolivia")

    def test_country_falls_back_to_the_code(self):
        self.assertEqual(taxonomy.country_name("ZZ", ""), "ZZ")
        self.assertEqual(taxonomy.country_name("ZZ", "ZZ"), "ZZ")
        self.assertEqual(taxonomy.country_name("", "Nowhere"), "Nowhere")

    def test_only_obvious_non_genres_are_dropped(self):
        # These are the tags that reach the top of a count-ranked list while
        # describing the station rather than the music.
        for tag in ("fm", "FM", "radio", "hd", "live", "stream"):
            self.assertFalse(taxonomy.is_genre(tag), tag)
        # A genre is never dropped, however odd it looks.
        for tag in ("pop", "oldies", "hip hop", "80s", "música", "variety"):
            self.assertTrue(taxonomy.is_genre(tag), tag)


class CountryCodes(unittest.TestCase):
    """iptv-org publishes bare codes and no names, and files the UK as "UK".

    So the TV filter offered "UK", "DO" and "CZ" with nothing to read, and
    typing GB - which is what most of the world calls it - matched nothing at
    all and came back as an empty list.
    """

    def test_codes_get_readable_labels(self):
        self.assertEqual(taxonomy.country_label("UK"), "United Kingdom")
        self.assertEqual(taxonomy.country_label("DO"), "Dominican Republic")
        self.assertEqual(taxonomy.country_label("se"), "Sweden")
        # An unknown code falls back to itself, which is what it always was.
        self.assertEqual(taxonomy.country_label("ZZ"), "ZZ")

    def test_a_code_stays_a_code(self):
        # The obvious implementation looks the input up in the name table and
        # turns "SE" into "Sweden", which then filters for nothing at all.
        for code in ("SE", "DE", "US", "IN", "BR"):
            self.assertEqual(taxonomy.country_code(code), code)
            self.assertEqual(taxonomy.country_code(code.lower()), code)

    def test_aliases_resolve_to_the_stored_code(self):
        self.assertEqual(taxonomy.country_code("GB"), "UK")
        self.assertEqual(taxonomy.country_code("United Kingdom"), "UK")
        self.assertEqual(taxonomy.country_code("united states"), "US")
        self.assertEqual(taxonomy.country_code("Korea"), "KR")

    def test_an_unknown_value_passes_through(self):
        self.assertEqual(taxonomy.country_code("ZZ"), "ZZ")
        self.assertEqual(taxonomy.country_code("germany"), "GERMANY")
        self.assertEqual(taxonomy.country_code(""), "")


class LegacyList(unittest.TestCase):
    def test_name_normalisation_groups_variants(self):
        self.assertEqual(legacy.normalise("BBC-Radio 1"),
                         legacy.normalise("BBC Radio 1"))
        self.assertNotEqual(legacy.normalise("Jazz FM"),
                            legacy.normalise("Jazz FM Live"))

    def test_placeholders_are_not_links(self):
        import csv as _csv
        import io
        _csv.field_size_limit(10 ** 7)
        text = ('STATION\tMESSAGE\tGENRE\tCOUNTRY\tLANGUAGE\tLINK1\tLINK2'
                '\tLINK3\tLINK4\tLINK5\tLINK6\n'
                'Real\t\tPop\tUK\tEnglish\thttp://a/1\thttp://a/2\t-\t-\t-\t-\n'
                'Empty\t\tPop\tUK\tEnglish\t-\t-\t-\t-\t-\t-\n')
        path = os.path.join(self.tmp(), "stations.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        rows = list(legacy.read(path))
        self.assertEqual([r[0] for r in rows], ["Real"])
        self.assertEqual(rows[0][1], ["http://a/1", "http://a/2"])

    def tmp(self):
        import tempfile
        return tempfile.mkdtemp(prefix="ap-legacy-")


class Lrc(unittest.TestCase):
    def test_parses_and_sorts_timestamps(self):
        lines = parse_lrc("[00:05.00]second\n[00:01.00]first\n[00:03.00]third\n")
        self.assertEqual([x["text"] for x in lines],
                         ["first", "third", "second"])
        self.assertEqual([x["t"] for x in lines], [1000, 3000, 5000])

    def test_ignores_untimed_but_keeps_instrumental_gaps(self):
        # A line with no timestamp is not a lyric. A *timed* line with no text
        # is an instrumental break, and dropping it would desync the rest.
        lines = parse_lrc("no timestamp here\n[00:01.00]\n[00:02.00]real\n")
        self.assertEqual([x["text"] for x in lines], ["", "real"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
