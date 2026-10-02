"""Casting: play on a TV, a speaker or a receiver on the local network.

DLNA/UPnP renderers are found with SSDP and driven with SOAP calls to their
AVTransport and RenderingControl services - the standard most smart TVs and
network speakers speak. Chromecasts and Google TVs are found through Avahi
(mDNS) and driven with Google's Cast protocol, implemented here: a TLS socket
carrying length-prefixed protobuf messages with JSON payloads.

Discovery has to get past the firewall. Omarchy runs ufw, which lets in
multicast to the SSDP and mDNS groups but drops the *unicast* answers to a
multicast M-SEARCH - they come from the device's address, which connection
tracking never saw us send to. So besides the multicast search, every address
on the local subnet is asked directly: each answer then matches a request we
made and is let through. mDNS goes through avahi-daemon, which listens on the
multicast group the firewall allows.

The network guard keeps every other part of the plugin away from private
addresses. Casting is the one thing that has to reach them, so it has the
opposite rule and no other: it only ever talks to an address that answered
the SSDP search from the local network, and only to private or link-local
addresses. A local file is served to the renderer by a one-file HTTP server
that answers one unguessable path, and only to the renderer's own address.
"""

import html
import http.server
import json
import shutil
import ssl
import struct
import subprocess
import ipaddress
import mimetypes
import os
import re
import secrets
import socket
import socketserver
import threading
import time
import urllib.parse
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

from ..util import textutil

SSDP_ADDR = ("239.255.255.250", 1900)
RENDERER = "urn:schemas-upnp-org:device:MediaRenderer:1"
AVTRANSPORT = "urn:schemas-upnp-org:service:AVTransport:1"
RENDERING = "urn:schemas-upnp-org:service:RenderingControl:1"
DESCRIPTION_LIMIT = 256 << 10
TIMEOUT = 5


class CastError(Exception):
    pass


def _private(host):
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_private or address.is_link_local


def _lan_get(url, limit=DESCRIPTION_LIMIT):
    """GET from a LAN device only. Redirects are not followed: a renderer
    has no business sending us elsewhere."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "http" or not _private(parts.hostname or ""):
        raise CastError("not a local network address")

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    opener = urllib.request.build_opener(NoRedirect)
    with opener.open(url, timeout=TIMEOUT) as response:
        return response.read(limit + 1)[:limit]


# -- discovery ---------------------------------------------------------------

def local_networks(limit_hosts=1024):
    """Our private IPv4 networks, as (our address, ip_network), from `ip`."""
    out = []
    try:
        data = json.loads(subprocess.run(["ip", "-j", "-4", "addr"], capture_output=True,
                                         text=True, timeout=3).stdout or "[]")
    except (OSError, ValueError, subprocess.SubprocessError):
        return out
    for link in data:
        if link.get("ifname") == "lo" or "UP" not in (link.get("flags") or []):
            continue
        for info in link.get("addr_info") or []:
            try:
                net = ipaddress.ip_network("%s/%s" % (info["local"], info["prefixlen"]),
                                           strict=False)
            except (KeyError, ValueError):
                continue
            if net.is_private and net.num_addresses <= limit_hosts + 2:
                out.append((info["local"], net))
            elif net.is_private:
                # A big network: only our own /24 of it.
                out.append((info["local"], ipaddress.ip_network(
                    "%s/24" % info["local"], strict=False)))
    return out


def _ssdp(timeout=3.0, target=RENDERER, sweep=True):
    """LOCATION URLs from every renderer that answers, keyed by address."""
    def message(host):
        return ("M-SEARCH * HTTP/1.1\r\nHOST: %s:1900\r\n"
                "MAN: \"ssdp:discover\"\r\nMX: 2\r\nST: %s\r\n\r\n"
                % (host, target)).encode()
    found = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        for _ in range(2):              # UDP: ask twice
            sock.sendto(message("239.255.255.250"), SSDP_ADDR)
        if sweep:
            # Non-blocking: a send to an address nobody has answered ARP for
            # yet would otherwise wait, and a /24 of them took nine seconds.
            sock.setblocking(False)
            for own, net in local_networks():
                for host in net.hosts():
                    if str(host) != own:
                        try:
                            sock.sendto(message(str(host)), (str(host), 1900))
                        except OSError:
                            pass
        sock.settimeout(0.3)
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                data, (host, _port) = sock.recvfrom(8192)
            except socket.timeout:
                continue
            if not _private(host):
                continue
            match = re.search(rb"(?im)^location:\s*(\S+)", data)
            if not match:
                continue
            location = match.group(1).decode("latin-1")[:512]
            if urllib.parse.urlsplit(location).hostname == host:
                found.setdefault(host, location)
    except OSError:
        pass
    finally:
        sock.close()
    return found


def _avahi(service, timeout=5):
    """Resolved mDNS services through avahi-daemon: [{name, host, port, txt}]."""
    if not shutil.which("avahi-browse"):
        return []
    try:
        out = subprocess.run(["avahi-browse", "-prt", service], capture_output=True,
                             text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    found = {}
    for line in out.splitlines():
        parts = line.split(";")
        if len(parts) < 10 or parts[0] != "=" or parts[2] != "IPv4":
            continue
        txt = {}
        for item in re.findall(r'"([^"]*)"', ";".join(parts[9:])):
            key, _, value = item.partition("=")
            txt[key] = value
        address = parts[7]
        if not _private(address):
            continue
        try:
            port = int(parts[8])
        except ValueError:
            continue
        found.setdefault(address, {"name": parts[3], "host": address, "port": port, "txt": txt})
    return list(found.values())


def _strip_ns(tree):
    for node in tree.iter():
        if isinstance(node.tag, str) and "}" in node.tag:
            node.tag = node.tag.split("}", 1)[1]
    return tree


def describe(location):
    """A renderer's name and control URLs, from its description XML."""
    body = _lan_get(location)
    try:
        root = _strip_ns(ET.fromstring(body))
    except ET.ParseError as exc:
        raise CastError("unreadable device description: %s" % exc)
    device = root.find(".//device")
    if device is None:
        raise CastError("no device in its description")
    base = root.findtext("URLBase") or location
    out = {"id": "dlna:" + textutil.text(device.findtext("UDN") or location, 120),
           "name": textutil.text(device.findtext("friendlyName") or "Renderer", 80),
           "model": textutil.text(device.findtext("modelName") or "", 80),
           "kind": "dlna", "location": location}
    for service in root.iter("service"):
        kind = service.findtext("serviceType") or ""
        control = service.findtext("controlURL") or ""
        url = urllib.parse.urljoin(base, control)
        if urllib.parse.urlsplit(url).hostname != urllib.parse.urlsplit(location).hostname:
            continue                     # control must stay on the same device
        if kind.startswith("urn:schemas-upnp-org:service:AVTransport:"):
            out["avtransport"] = url
            out["avtransport_type"] = kind
        elif kind.startswith("urn:schemas-upnp-org:service:RenderingControl:"):
            out["rendering"] = url
            out["rendering_type"] = kind
    if "avtransport" not in out:
        raise CastError("%s cannot be told what to play" % out["name"])
    return out


def chromecast_available():
    """Chromecasts are found through avahi-daemon."""
    return bool(shutil.which("avahi-browse"))


def chromecasts(timeout=5):
    out = []
    for service in _avahi("_googlecast._tcp", timeout):
        txt = service["txt"]
        out.append({"id": "cc:%s" % textutil.text(txt.get("id") or service["host"], 64),
                    "kind": "chromecast",
                    "name": textutil.text(txt.get("fn") or service["name"], 80),
                    "model": textutil.text(txt.get("md") or "Chromecast", 80),
                    "host": service["host"], "port": service["port"]})
    return out


def discover(timeout=3.0):
    """Every renderer on the local network, DLNA and Chromecast, found in
    parallel."""
    devices = []
    lock = threading.Lock()

    def dlna():
        for _host, location in _ssdp(timeout).items():
            try:
                found = describe(location)
            except (CastError, OSError, ValueError):
                continue
            with lock:
                devices.append(found)

    def cast():
        found = chromecasts(timeout + 2)
        with lock:
            devices.extend(found)

    threads = [threading.Thread(target=dlna, daemon=True),
               threading.Thread(target=cast, daemon=True)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout + 10)
    with lock:
        result = list(devices)
    result.sort(key=lambda d: d["name"].lower())
    return result


# -- DLNA control --------------------------------------------------------------

def _soap(url, service, action, args):
    if not _private(urllib.parse.urlsplit(url).hostname or ""):
        raise CastError("not a local network address")
    body = "".join("<%s>%s</%s>" % (k, html.escape(str(v), quote=False), k)
                   for k, v in args)
    envelope = ('<?xml version="1.0" encoding="utf-8"?>'
                '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
                's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
                '<s:Body><u:%s xmlns:u="%s">%s</u:%s></s:Body></s:Envelope>'
                % (action, service, body, action)).encode("utf-8")
    request = urllib.request.Request(url, data=envelope, method="POST", headers={
        "Content-Type": 'text/xml; charset="utf-8"',
        "SOAPAction": '"%s#%s"' % (service, action)})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.read(DESCRIPTION_LIMIT)
    except urllib.error.HTTPError as exc:
        detail = exc.read(4096).decode("utf-8", "replace")
        code = re.search(r"<errorDescription>([^<]+)", detail) or \
            re.search(r"<errorCode>([^<]+)", detail)
        raise CastError("%s refused %s%s" % (urllib.parse.urlsplit(url).hostname, action,
                                             ": " + code.group(1) if code else ""))
    except OSError as exc:
        raise CastError("the device did not answer: %s" % exc)


def didl(title, artist, url, mime, art=""):
    """DIDL-Lite metadata: what most TVs show while playing."""
    kind = "object.item.videoItem" if mime.startswith("video/") else "object.item.audioItem.musicTrack"
    esc = lambda v: html.escape(str(v or ""), quote=True)  # noqa: E731
    return ('<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
            '<item id="0" parentID="-1" restricted="1">'
            '<dc:title>%s</dc:title><upnp:artist>%s</upnp:artist>'
            '<upnp:class>%s</upnp:class>%s'
            '<res protocolInfo="http-get:*:%s:*">%s</res></item></DIDL-Lite>'
            % (esc(title), esc(artist), kind,
               ('<upnp:albumArtURI>%s</upnp:albumArtURI>' % esc(art)) if art else "",
               esc(mime), esc(url)))


class Dlna:
    def __init__(self, device):
        self.device = device

    def _av(self, action, args=()):
        return _soap(self.device["avtransport"], self.device.get("avtransport_type", AVTRANSPORT),
                     action, [("InstanceID", 0)] + list(args))

    def play(self, url, title="", artist="", mime="audio/mpeg", art=""):
        try:
            self._av("Stop")
        except CastError:
            pass                         # nothing was playing
        self._av("SetAVTransportURI", [("CurrentURI", url),
                                       ("CurrentURIMetaData", didl(title, artist, url, mime, art))])
        self._av("Play", [("Speed", 1)])

    def pause(self):
        self._av("Pause")

    def resume(self):
        self._av("Play", [("Speed", 1)])

    def stop(self):
        self._av("Stop")

    def volume(self, percent):
        if "rendering" not in self.device:
            return
        _soap(self.device["rendering"], self.device.get("rendering_type", RENDERING),
              "SetVolume", [("InstanceID", 0), ("Channel", "Master"),
                            ("DesiredVolume", max(0, min(100, int(percent))))])


# -- Google Cast ----------------------------------------------------------------

CAST_PORT = 8009
MEDIA_RECEIVER = "CC1AD845"           # Google's default media receiver app
NS_CONNECTION = "urn:x-cast:com.google.cast.tp.connection"
NS_HEARTBEAT = "urn:x-cast:com.google.cast.tp.heartbeat"
NS_RECEIVER = "urn:x-cast:com.google.cast.receiver"
NS_MEDIA = "urn:x-cast:com.google.cast.media"
CAST_MESSAGE_LIMIT = 64 << 10


def _varint(value):
    out = b""
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out += bytes([byte | 0x80])
        else:
            return out + bytes([byte])


def encode_cast(source, destination, namespace, payload):
    """A CastMessage protobuf: version, source, destination, namespace,
    payload type STRING, and the JSON payload."""
    def text(field, value):
        data = value.encode("utf-8")
        return _varint(field << 3 | 2) + _varint(len(data)) + data
    body = (_varint(1 << 3) + _varint(0) + text(2, source) + text(3, destination)
            + text(4, namespace) + _varint(5 << 3) + _varint(0)
            + text(6, json.dumps(payload, separators=(",", ":"))))
    return struct.pack(">I", len(body)) + body


def decode_cast(body):
    """A CastMessage protobuf into {source, destination, namespace, payload}."""
    out, i = {}, 0
    names = {2: "source", 3: "destination", 4: "namespace", 6: "payload"}

    def varint():
        nonlocal i
        shift = value = 0
        while True:
            if i >= len(body):
                raise CastError("truncated message")
            byte = body[i]
            i += 1
            value |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return value
            shift += 7
            if shift > 63:
                raise CastError("bad varint")

    while i < len(body):
        key = varint()
        field, kind = key >> 3, key & 7
        if kind == 0:
            varint()
        elif kind == 2:
            length = varint()
            data = body[i:i + length]
            i += length
            if field in names:
                out[names[field]] = data.decode("utf-8", "replace")
        else:
            raise CastError("unexpected field type %d" % kind)
    try:
        out["payload"] = json.loads(out.get("payload") or "{}")
    except ValueError:
        out["payload"] = {}
    return out


class Chromecast:
    """Play one thing on a Chromecast or Google TV with the default media
    receiver. A reader thread answers the device's heartbeats and keeps the
    media session id current."""

    def __init__(self, device):
        host = device.get("host", "")
        if not _private(host):
            raise CastError("not a local network address")
        context = ssl.create_default_context()
        # Cast devices present a certificate signed by Google's device CA,
        # not one a browser trusts; the address is the one that answered on
        # the local network, which is what we are trusting.
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        raw = socket.create_connection((host, int(device.get("port") or CAST_PORT)),
                                       timeout=TIMEOUT)
        self.sock = context.wrap_socket(raw)
        self.sock.settimeout(TIMEOUT)
        self.name = device.get("name", "Chromecast")
        self._lock = threading.Lock()
        self._request = 0
        self._replies = {}
        self._cond = threading.Condition()
        self.transport = ""
        self.session = ""
        self.media_session = None
        self.closed = False
        self._send("receiver-0", NS_CONNECTION, {"type": "CONNECT"})
        threading.Thread(target=self._read, name="ap-cast-read", daemon=True).start()
        threading.Thread(target=self._beat, name="ap-cast-beat", daemon=True).start()

    def _send(self, destination, namespace, payload):
        frame = encode_cast("sender-0", destination, namespace, payload)
        with self._lock:
            self.sock.sendall(frame)

    def _ask(self, destination, namespace, payload, want, seconds=10):
        """Send a request and wait for a reply of one of the `want` types."""
        with self._cond:
            self._request += 1
            ident = self._request
        payload = dict(payload, requestId=ident)
        self._send(destination, namespace, payload)
        end = time.monotonic() + seconds
        with self._cond:
            while ident not in self._replies:
                left = end - time.monotonic()
                if left <= 0 or self.closed:
                    raise CastError("%s did not answer" % self.name)
                self._cond.wait(left)
            reply = self._replies.pop(ident)
        if reply.get("type") not in want:
            raise CastError("%s refused: %s" % (self.name, reply.get("reason")
                                                 or reply.get("type")))
        return reply

    def _read(self):
        try:
            while not self.closed:
                head = self._exactly(4)
                length = struct.unpack(">I", head)[0]
                if length > CAST_MESSAGE_LIMIT:
                    raise CastError("message too large")
                message = decode_cast(self._exactly(length))
                payload = message.get("payload") or {}
                kind = payload.get("type")
                if kind == "PING":
                    self._send(message.get("source", "receiver-0"), NS_HEARTBEAT,
                               {"type": "PONG"})
                    continue
                if kind == "CLOSE":
                    continue
                if kind == "MEDIA_STATUS":
                    for status in payload.get("status") or []:
                        if status.get("mediaSessionId") is not None:
                            self.media_session = status["mediaSessionId"]
                with self._cond:
                    if payload.get("requestId"):
                        self._replies[payload["requestId"]] = payload
                    self._cond.notify_all()
        except (OSError, CastError, ValueError):
            pass
        finally:
            self.closed = True
            with self._cond:
                self._cond.notify_all()

    def _exactly(self, count):
        data = b""
        while len(data) < count:
            try:
                chunk = self.sock.recv(count - len(data))
            except socket.timeout:
                if self.closed:
                    raise CastError("closed")
                continue
            if not chunk:
                raise CastError("the device closed the connection")
            data += chunk
        return data

    def _beat(self):
        while not self.closed:
            time.sleep(5)
            try:
                self._send("receiver-0", NS_HEARTBEAT, {"type": "PING"})
            except OSError:
                self.closed = True

    def play(self, url, title="", artist="", mime="audio/mpeg", art=""):
        status = self._ask("receiver-0", NS_RECEIVER,
                           {"type": "LAUNCH", "appId": MEDIA_RECEIVER},
                           ("RECEIVER_STATUS",), seconds=20)
        app = next((a for a in (status.get("status") or {}).get("applications") or []
                    if a.get("appId") == MEDIA_RECEIVER), None)
        if app is None:
            raise CastError("%s would not start its media player" % self.name)
        self.transport, self.session = app.get("transportId", ""), app.get("sessionId", "")
        self._send(self.transport, NS_CONNECTION, {"type": "CONNECT"})
        metadata = {"metadataType": 0, "title": title, "subtitle": artist}
        if art:
            metadata["images"] = [{"url": art}]
        live = "mpegurl" in mime         # HLS: a TV channel
        self._ask(self.transport, NS_MEDIA, {
            "type": "LOAD", "autoplay": True, "currentTime": 0,
            "media": {"contentId": url, "contentType": mime, "metadata": metadata,
                      "streamType": "LIVE" if live else "BUFFERED"}},
            ("MEDIA_STATUS",), seconds=25)

    def _media(self, kind):
        if self.media_session is None:
            raise CastError("nothing is playing on %s" % self.name)
        self._ask(self.transport, NS_MEDIA, {"type": kind,
                                             "mediaSessionId": self.media_session},
                  ("MEDIA_STATUS",))

    def pause(self):
        self._media("PAUSE")

    def resume(self):
        self._media("PLAY")

    def stop(self):
        try:
            if self.session:
                self._ask("receiver-0", NS_RECEIVER,
                          {"type": "STOP", "sessionId": self.session},
                          ("RECEIVER_STATUS",), seconds=5)
        finally:
            self.closed = True
            try:
                self.sock.close()
            except OSError:
                pass

    def volume(self, percent):
        self._ask("receiver-0", NS_RECEIVER,
                  {"type": "SET_VOLUME",
                   "volume": {"level": max(0, min(100, int(percent))) / 100.0}},
                  ("RECEIVER_STATUS",))


def controller(device):
    if device.get("kind") == "chromecast":
        return Chromecast(device)
    return Dlna(device)


# -- serving a local file ------------------------------------------------------

def local_address_for(host):
    """Our address on the interface that reaches `host`."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect((host, 9))         # no packet is sent for UDP connect
        return probe.getsockname()[0]
    finally:
        probe.close()


class FileServer:
    """Serves one file, at one secret path, to one address. Range requests
    are honoured, because a TV seeks by asking for a byte range."""

    def __init__(self, path, client, bind=""):
        self.path = path
        self.client = client
        self.token = secrets.token_urlsafe(18)
        self.mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            server_version = "AuroraPulse"
            sys_version = ""

            def log_message(self, *args):
                pass

            def _allowed(self):
                return (self.client_address[0] == server.client and
                        self.path.split("?")[0] == "/cast/%s/%s" % (
                            server.token, urllib.parse.quote(os.path.basename(server.path))))

            def do_HEAD(self):
                self.do_GET(head=True)

            def do_GET(self, head=False):
                if not self._allowed():
                    self.send_error(404)
                    return
                try:
                    size = os.path.getsize(server.path)
                    handle = open(server.path, "rb")
                except OSError:
                    self.send_error(404)
                    return
                with handle:
                    start, end = 0, size - 1
                    match = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
                    if match and (match.group(1) or match.group(2)):
                        if match.group(1):
                            start = int(match.group(1))
                            end = int(match.group(2)) if match.group(2) else size - 1
                        else:
                            start = max(0, size - int(match.group(2)))
                        end = min(end, size - 1)
                        if start > end:
                            self.send_error(416)
                            return
                        self.send_response(206)
                        self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
                    else:
                        self.send_response(200)
                    self.send_header("Content-Type", server.mime)
                    self.send_header("Content-Length", str(end - start + 1))
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("transferMode.dlna.org", "Streaming")
                    self.end_headers()
                    if head:
                        return
                    handle.seek(start)
                    left = end - start + 1
                    try:
                        while left > 0:
                            chunk = handle.read(min(256 << 10, left))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            left -= len(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        pass

        class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True
            allow_reuse_address = True

        self.httpd = Server((bind or local_address_for(client), 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="ap-cast",
                                       daemon=True)
        self.thread.start()

    @property
    def url(self):
        host, port = self.httpd.server_address[:2]
        return "http://%s:%d/cast/%s/%s" % (host, port, self.token,
                                            urllib.parse.quote(os.path.basename(self.path)))

    def close(self):
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except OSError:
            pass


def device_host(device):
    if device.get("kind") == "chromecast":
        return device.get("host", "")
    return urllib.parse.urlsplit(device.get("location", "")).hostname or ""
