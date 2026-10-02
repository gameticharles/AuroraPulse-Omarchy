"""Network guard.

Nothing in this plugin reaches the network without passing through here. The
policy is: globally routable public unicast only, reached through a default
route, on an allow-listed port, over a connection made to the *exact* address we
vetted. That last part is what closes the DNS-rebinding window.

mpv resolves names, follows redirects and expands playlists itself, so the
player talks to a local proxy (see core/session.py) and every redirect, every
HLS segment and every playlist entry is re-checked by this module.
"""

import http.client
import ipaddress
import json
import os
import socket
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# The WHATWG "bad ports" list. Even if a service on the user's machine is
# listening on one of these, the plugin must never be the thing that reaches it.
BAD_PORTS = frozenset({
    1, 7, 9, 11, 13, 15, 17, 19, 20, 21, 22, 23, 25, 37, 42, 43, 53, 69, 77,
    79, 87, 95, 101, 102, 103, 104, 109, 110, 111, 113, 115, 117, 119, 123,
    135, 137, 138, 139, 143, 161, 179, 389, 427, 465, 512, 513, 514, 515,
    526, 530, 531, 532, 540, 548, 554, 556, 563, 587, 601, 636, 989, 990,
    993, 995, 1719, 1720, 1723, 2049, 3659, 4045, 4190, 5060, 5061, 6000,
    6566, 6665, 6666, 6667, 6668, 6669, 6679, 6697, 10080,
})

API_PORTS = (443,)
WEB_PORTS = (80, 443)

# Media playback gets a wider port policy than ordinary web requests, and the
# reason is that the allow-list was quietly costing a fifth of the TV
# catalogue. Plenty of channels stream from an odd port - 8000, 1935, 8080,
# 8989 - and every one of them failed with "port is not on the allow-list",
# which reads like the plugin was broken rather than like a policy decision.
#
# The port restriction was never the real defence. What stops a media URL from
# reaching something on the user's own machine is BAD_PORTS plus the address
# checks: loopback, private, link-local and CGNAT ranges are all refused, and
# the connect is pinned to the exact address that was vetted, so there is no
# window for a rebind. `None` means "any port that is not on the blocked list".
MEDIA_PORTS = None

NAT64 = ipaddress.ip_network("64:ff9b::/96")
IPV4_COMPATIBLE = ipaddress.ip_network("::/96")


class Blocked(Exception):
    """A request was refused by policy. Never retried without a reason."""


class HTTPStatus(Blocked):
    """The server answered, but not with 200.

    Subclasses Blocked so a caller's existing handler still catches it, but
    carries the code so the shell can say "that is gone" instead of the much
    more alarming "blocked".
    """

    def __init__(self, status, url=""):
        self.status = int(status)
        self.url = url
        super().__init__("the server answered %d" % self.status)


def _is_public_v4(address):
    if address.is_loopback or address.is_link_local or address.is_private:
        return False
    if address.is_multicast or address.is_reserved or address.is_unspecified:
        return False
    if address in ipaddress.ip_network("100.64.0.0/10"):  # CGNAT
        return False
    if address in ipaddress.ip_network("192.0.0.0/24"):  # IETF protocol
        return False
    if address in ipaddress.ip_network("198.18.0.0/15"):  # benchmarking
        return False
    if address in ipaddress.ip_network("240.0.0.0/4"):  # reserved
        return False
    return True


def public_ip(address):
    """True only for globally routable unicast.

    An IPv6 form that carries an IPv4 address has to carry a public one, or it
    is a tunnel straight to the machine's LAN.
    """
    try:
        address = ipaddress.ip_address(address)
    except ValueError:
        return False

    if address.version == 4:
        return _is_public_v4(address)

    # fc00::/7 is unique-local (RFC 4193) and ::1/128 is loopback. Python's
    # is_private covers fc00::/7, but being explicit here means the rule is
    # visible in the code rather than a library detail someone has to know.
    if address in ipaddress.ip_network("fc00::/7"):
        return False
    if address.is_loopback or address.is_link_local or address.is_multicast:
        return False
    if address.is_reserved or address.is_unspecified or address.is_private:
        return False

    if address.ipv4_mapped:
        return _is_public_v4(address.ipv4_mapped)
    if address in IPV4_COMPATIBLE or address in NAT64:
        return _is_public_v4(ipaddress.IPv4Address(int(address) & 0xFFFFFFFF))

    embedded = []
    if address.sixtofour:
        embedded.append(address.sixtofour)
    if address.teredo:
        embedded.extend([address.teredo[1], address.teredo[0]])
    for four in embedded:
        if not _is_public_v4(four):
            return False
    return True


_ROUTE_CACHE = {"at": 0.0, "devices": frozenset()}
_ROUTE_LOCK = threading.Lock()


def _default_devices():
    """Devices that carry a default route, from `ip -j route show default`."""
    out = set()
    try:
        result = subprocess.run(
            ["ip", "-j", "route", "show", "default"],
            capture_output=True, timeout=5,
        )
        for row in json.loads(result.stdout or b"[]"):
            dev = row.get("dev")
            if dev and dev != "lo":
                out.add(dev)
    except Exception:
        # Fail closed: with no view of the routing table we cannot tell where
        # a connection goes, so we treat every device as non-default.
        return frozenset()
    return frozenset(out)


def routes_to_this_machine(address):
    """True when the address would be reached through a normal default route.

    A point-to-point default route with no gateway (a full-tunnel VPN, PPP) is
    allowed, because that genuinely leaves the machine. Anything that resolves
    to a local, loopback, multicast or broadcast destination, or that goes out
    of a device with no default route, is refused.
    """
    now = time.monotonic()
    with _ROUTE_LOCK:
        if now - _ROUTE_CACHE["at"] > 60:
            _ROUTE_CACHE["at"] = now
            _ROUTE_CACHE["devices"] = _default_devices()
        devices = _ROUTE_CACHE["devices"]

    if not devices:
        return False

    family = "-4" if ipaddress.ip_address(address).version == 4 else "-6"
    try:
        result = subprocess.run(
            ["ip", "-j", family, "route", "get", str(address)],
            capture_output=True, timeout=5,
        )
        rows = json.loads(result.stdout or b"[]")
    except Exception:
        return False
    if not rows:
        return False

    row = rows[0]
    kind = row.get("type")
    if kind in ("local", "broadcast", "multicast", "unreachable", "blackhole",
                "prohibit", "throw"):
        return False
    dev = row.get("dev")
    if dev == "lo":
        return False
    if dev and dev not in devices:
        return False
    # A gateway is normal. Its absence on a default device means a tunnel.
    return True


def _resolve_public(host, port, seconds):
    """Resolve, then return the list of vetted public (family, addr) pairs."""
    holder = {}

    def worker():
        try:
            infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
        except Exception as exc:
            holder["error"] = exc
            return
        holder["infos"] = infos

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout=seconds)
    if thread.is_alive():
        raise Blocked("DNS lookup for %s did not finish in time" % host)
    if "error" in holder:
        raise Blocked("could not resolve %s" % host)

    vetted = []
    for family, _type, _proto, _canon, sockaddr in holder["infos"]:
        address = sockaddr[0]
        if not public_ip(address):
            raise Blocked("%s resolves to %s, which is not the public internet"
                          % (host, address))
        if not routes_to_this_machine(address):
            raise Blocked("%s (%s) would not leave through a default route"
                          % (host, address))
        vetted.append((family, address))
    if not vetted:
        raise Blocked("%s has no usable address" % host)
    return vetted


def _check_port(port, allowed):
    port = int(port)
    if allowed is None:
        # Media policy: anything not explicitly blocked. The address checks,
        # not this list, are what keep a URL off the user's own network.
        if port in BAD_PORTS:
            raise Blocked("port %d is on the blocked-ports list" % port)
        return port
    if port in allowed:
        return port
    if port in BAD_PORTS:
        raise Blocked("port %d is on the blocked-ports list" % port)
    raise Blocked("port %d is not on the allow-list %s" % (port, list(allowed)))


def public_connection(host, port, allowed, seconds=10):
    """A socket already connected to a vetted address, or Blocked.

    The connect() happens here rather than in urllib so that there is no window
    between checking the address and using it.
    """
    if port is None:
        port = 443 if allowed == API_PORTS else 80
    _check_port(port, allowed)

    last = None
    for family, address in _resolve_public(host, port, seconds):
        try:
            sock = socket.socket(family, socket.SOCK_STREAM)
            sock.settimeout(seconds)
            sock.connect((address, port))
            return sock
        except Exception as exc:
            last = exc
            continue
    raise Blocked("could not reach %s:%s (%s)" % (host, port, last))


class Deadline:
    """A wall-clock budget that also unblocks a socket stuck mid-read.

    A watchdog shuts every tracked socket down. That looks exactly like a clean
    EOF to the reader, which is why bounded_read() re-checks after the read
    rather than trusting the return.
    """

    def __init__(self, seconds):
        self.seconds = max(1.0, float(seconds))
        self._socks = []
        self._lock = threading.Lock()
        self._timers = []

    def track(self, sock):
        with self._lock:
            self._socks.append(sock)
        return sock

    def __enter__(self):
        def fire():
            with self._lock:
                socks = list(self._socks)
            for sock in socks:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

        # Timers accumulate rather than overwrite, so a nested fetch() inside
        # an outer budget() still leaves the outer watchdog armed on exit.
        timer = threading.Timer(self.seconds, fire)
        timer.daemon = True
        self._timers.append(timer)
        timer.start()
        return self

    def __exit__(self, *exc):
        for timer in self._timers:
            timer.cancel()
        del self._timers[:]
        return False


def _build_opener(allowed, https_only):
    handlers = [
        urllib.request.UnknownHandler,
        urllib.request.HTTPDefaultErrorHandler,
        urllib.request.HTTPRedirectHandler,
        urllib.request.HTTPErrorProcessor,
    ]
    if not https_only:
        handlers.append(urllib.request.HTTPHandler)
    # No ProxyHandler, no FTPHandler, no FileHandler: the opener can only
    # speak to the network, and only over the protocol we asked for.
    opener = urllib.request.build_opener(*handlers)
    opener.addheaders = [("User-Agent", USER_AGENT), ("Accept-Encoding", "identity")]
    return opener, allowed


USER_AGENT = "AuroraPulse/0.1 (Omarchy)"

API_OPENER, _API_PORTS_REF = _build_opener(API_PORTS, https_only=True)
ART_OPENER, _ART_PORTS_REF = _build_opener(WEB_PORTS, https_only=False)

_OFFLINE = {"on": False}
_offline_lock = threading.Lock()

REDIRECT_LIMIT = 5


def set_offline(enabled):
    with _offline_lock:
        _OFFLINE["on"] = bool(enabled)


def offline():
    with _offline_lock:
        return _OFFLINE["on"]


# -- IP-pinned transport --------------------------------------------------
#
# Vetting a hostname and then handing the name to urllib would leave a
# rebinding window: the second lookup could answer with 127.0.0.1. So the
# transport connects to the address we already vetted, and only the Host
# header and TLS server name still use the original hostname.

class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host, ip, port, timeout):
        self._pinned_ip = ip
        super().__init__(host, port, timeout=timeout)

    def connect(self):
        self.sock = socket.create_connection(
            (self._pinned_ip, self.port), self.timeout, self.source_address)
        if self._tunnel_host:
            self._tunnel()


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, ip, port, timeout, context):
        self._pinned_ip = ip
        super().__init__(host, port=port, timeout=timeout, context=context)

    def connect(self):
        raw = socket.create_connection(
            (self._pinned_ip, self.port), self.timeout, self.source_address)
        # server_hostname stays the real name: that is what SNI and the
        # certificate check must see, even though the socket went to the IP.
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


def _get_pinned(url, seconds, allowed, track=None):
    """One GET, pinned to a vetted address. Returns (status, headers, body)."""
    split = urllib.parse.urlsplit(url)
    host = split.hostname or ""
    scheme = (split.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise Blocked("only http and https are allowed")
    if not host:
        raise Blocked("that URL has no host")
    try:
        host.encode("idna")
    except (UnicodeError, ValueError):
        raise Blocked("that host name is not valid")

    port = split.port or (443 if scheme == "https" else 80)
    _check_port(port, allowed)

    path = split.path or "/"
    if split.query:
        path += "?" + split.query

    # Try each vetted address; a v6 host on a v4-only link just moves on.
    last = None
    for _family, address in _resolve_public(host, port, seconds):
        if scheme == "https":
            connection = _PinnedHTTPS(host, address, port, seconds,
                                      ssl.create_default_context())
        else:
            connection = _PinnedHTTP(host, address, port, seconds)
        try:
            connection.connect()
            if track is not None:
                track(connection.sock)
            connection.request("GET", path, headers={
                "Host": host if port in (80, 443) else "%s:%d" % (host, port),
                "User-Agent": USER_AGENT,
                "Accept-Encoding": "identity",
                "Connection": "close",
            })
            response = connection.getresponse()
            body = response.read()
            return response.status, response, body
        except Blocked:
            raise
        except Exception as exc:
            last = exc
        finally:
            try:
                connection.close()
            except Exception:
                pass
    raise Blocked("could not reach %s:%d (%s)" % (host, port, last))


def post(url, body, content_type, limit=4 << 20, seconds=20, allowed=None,
         headers=None):
    """POST `body` (bytes) to a vetted https URL; returns (status, bytes).

    The same pinning as a GET: the host is resolved once, every address is
    checked to be public, and the connection goes to the checked address.
    Redirects are not followed at all - an API that redirects a POST is not
    one this needs to talk to.
    """
    if offline():
        raise Blocked("offline mode is on")
    split = urllib.parse.urlsplit(url)
    host = split.hostname or ""
    if (split.scheme or "").lower() != "https" or not host:
        raise Blocked("only https is allowed for this")
    port = split.port or 443
    _check_port(port, allowed if allowed is not None else WEB_PORTS)
    path = (split.path or "/") + ("?" + split.query if split.query else "")
    last = None
    for _family, address in _resolve_public(host, port, seconds):
        connection = _PinnedHTTPS(host, address, port, seconds,
                                  ssl.create_default_context())
        try:
            connection.connect()
            sent = {"Host": host, "User-Agent": "AuroraPulse/0.2 (Omarchy)",
                    "Content-Type": content_type,
                    "Accept-Encoding": "identity", "Connection": "close",
                    "Content-Length": str(len(body))}
            sent.update(headers or {})
            connection.request("POST", path, body=body, headers=sent)
            response = connection.getresponse()
            data = response.read(limit + 1)
            if len(data) > limit:
                raise Blocked("the reply was too large")
            return response.status, data
        except Blocked:
            raise
        except Exception as exc:
            last = exc
        finally:
            try:
                connection.close()
            except Exception:
                pass
    raise Blocked("could not reach %s (%s)" % (host, last))


def post_json(url, payload, limit=4 << 20, seconds=20, allowed=None,
              headers=None):
    """POST a JSON body and return the decoded JSON reply (HTTP 200 only)."""
    sent = {"User-Agent": "Mozilla/5.0"}
    sent.update(headers or {})
    status, data = post(url, json.dumps(payload).encode("utf-8"),
                        "application/json", limit, seconds, allowed, sent)
    if status != 200:
        raise OSError("HTTP %d" % status)
    return json.loads(data.decode("utf-8", "replace"))


def fetch(url, limit=4 << 20, seconds=20, opener=None, allowed=None,
          deadline=None, https_only=True):
    """GET a vetted URL, capped in size and in time.

    Every hop is re-vetted: a redirect to 127.0.0.1 is refused rather than
    followed, and https can never be bounced down to http.
    """
    return fetch_sized(url, limit=limit, seconds=seconds, opener=opener,
                       allowed=allowed, deadline=deadline,
                       https_only=https_only)[0]


def fetch_sized(url, limit=4 << 20, seconds=20, opener=None, allowed=None,
                deadline=None, https_only=True):
    """As `fetch`, and also the declared length of the body when there is one.

    Callers that show a progress bar need a denominator, and the only honest
    source for it is the server's own Content-Length. Guessing one from a page
    count produces a bar that reaches 100% and then keeps going, which is worse
    than no bar. Returns None for the length when the server did not say, so a
    caller can show an indeterminate figure instead of inventing a total.
    """
    if offline():
        raise Blocked("offline mode is on")
    if not isinstance(url, str):
        raise Blocked("that URL is not a string")
    if not url.lower().startswith(("http://", "https://")):
        raise Blocked("only http and https are allowed")

    allowed = allowed or (API_PORTS if https_only else WEB_PORTS)
    current = url
    body = b""
    total = None
    for _hop in range(REDIRECT_LIMIT + 1):
        scheme = current.split("://", 1)[0].lower()
        if https_only and scheme != "https":
            raise Blocked("only https is allowed here")
        with (deadline or Deadline(seconds)) as budget:
            status, response, body = _get_pinned(
                current, seconds, allowed, track=budget.track)
        if status in (301, 302, 303, 307, 308):
            location = response.getheader("Location")
            if not location:
                raise Blocked("redirect with no destination")
            current = urllib.parse.urljoin(current, location)
            if len(body) > limit:
                raise Blocked("redirect body is larger than %d bytes" % limit)
            continue
        if status != 200:
            raise HTTPStatus(status, current)
        # Taken from the response that actually carried the body, not the
        # request that was redirected away from.
        try:
            declared = response.getheader("Content-Length")
            total = int(declared) if declared is not None else None
        except (TypeError, ValueError):
            total = None
        break
    else:
        raise Blocked("too many redirects")

    if len(body) > limit:
        raise Blocked("response from %s is larger than %d bytes" % (url, limit))
    return body, total


def fetch_json(url, limit=4 << 20, seconds=20, opener=None, allowed=None):
    body = fetch(url, limit=limit, seconds=seconds, opener=opener, allowed=allowed)
    try:
        return json.loads(body.decode("utf-8", "replace"))
    except ValueError as exc:
        raise Blocked("response from %s is not JSON (%s)" % (url, exc))


def url_host(url):
    """The hostname of a URL: lowercased, no port, no userinfo, no trailing dot.

    Returns "" for anything unparseable rather than raising, because every
    caller uses this to recognise *which site* a URL is - a junk URL is simply
    not the site being looked for, and raising there would turn a bad link
    into a crash instead of a "not a YouTube link" answer.
    """
    try:
        host = urllib.parse.urlsplit(str(url)).hostname or ""
        host.encode("idna")
    except (ValueError, UnicodeError, AttributeError):
        return ""
    return host.lower().rstrip(".")


def public_url(url, seconds=10):
    """True when a URL is one we are willing to hand to a player."""
    try:
        split = str(url).split("://", 1)[1]
        host, _, rest = split.partition("/")
        host = host.partition("@")[2] or host
        host, _, raw_port = host.partition(":")
        port = int(raw_port) if raw_port.isdigit() else None
    except Exception:
        return False
    if port is not None and (port in BAD_PORTS or port in API_PORTS + WEB_PORTS):
        return False
    try:
        for family, address in _resolve_public(host, port or 443, seconds):
            if not public_ip(address):
                return False
    except Blocked:
        return False
    return True


def env_proxy():
    """The user's own proxy, if they set one, as (scheme, host, port)."""
    url = (os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
           or os.environ.get("http_proxy") or os.environ.get("HTTP_PROXY") or "")
    if not url:
        return None
    try:
        split = url.split("://", 1)[1]
        host, _, rest = split.partition("/")
        host = host.partition("@")[2] or host
        host, _, raw_port = host.partition(":")
        return host, int(raw_port) if raw_port.isdigit() else 8080
    except Exception:
        return None
