# Security Model

## 1. Threat model

AuroraPulse renders and plays material from sources the user does not control.
The attacker is anyone who can put a string or a URL in front of us.

| Surface | Untrusted? | Examples |
|---|---|---|
| Radio Browser JSON | yes | station names, tags, stream URLs, logo URLs |
| IPTV M3U playlists | yes | channel names, `tvg-logo` URLs, stream URLs, attributes |
| XMLTV EPG | yes | programme titles, descriptions, categories, icons |
| YouTube / YT Music | yes | titles, channel names, descriptions, thumbnails, format URLs, subtitles |
| Stream ICY metadata | yes | live-updating `StreamTitle` while a station is playing |
| Local media tags | partly | a `.mp3` downloaded from anywhere carries its own ID3 |
| Lyrics providers | yes | LRCLib / AZLyrics responses |
| The user | trusted | their own settings, their own files |

The consequences we care about: **code execution**, **reading the user's
files**, **reaching services on the user's LAN**, and **remote UI spoofing**.

## 2. Trust boundaries

```
┌─ omarchy-shell ────────────────────────────────────────────┐
│  Untrusted-by-host: third-party plugin code runs unsandboxed │
│  Our QML: no network, no file I/O, no child processes        │
│  ▲ newline-delimited JSON (no shell, no eval, no template)   │
└──────────────────────────────────────────────────────────────┘
┌─ ap-ctl daemon ────────────────────────────────────────────┐
│  TRUSTED. Owns all network and all file writes.             │
│  Sanitises every string that crosses back into the shell.   │
└──────────────────────────────────────────────────────────────┘
┌─ ap-ctl player session ────────────────────────────────────┐
│  Owns the proxy. Spawns the sandboxes. Does not parse media.│
└──────────────────────────────────────────────────────────────┘
┌─ sandboxes (audio / video / artwork) ──────────────────────┐
│  UNTRUSTED. See a hostile server here.                      │
└──────────────────────────────────────────────────────────────┘
```

## 3. The sandbox profiles

All of them are `bubblewrap` invocations sharing a base:

```
--unshare-all --unshare-user --disable-userns --cap-drop ALL
--die-with-parent --new-session --clearenv --hostname aurora
--ro-bind /usr /usr --dev /dev --remount-ro /dev --proc /proc
--ro-bind /dev/null /proc/cmdline
--tmpfs /tmp --tmpfs /run --dir /etc
--ro-bind-try {ld.so.cache,ssl,ca-certificates,pkcs11,gnutls,localtime,fonts}
--remount-ro /
```

A **seccomp-BPF** filter is compiled in Python and handed over as a
`memfd_create` descriptor (never a path, never a file in the image):

- `arch != x86_64` → `KILL`
- syscall numbers `>= 0x40000000` (x32 ABI) → `KILL`
- `socket`/`socketpair` → the address family must be `AF_UNIX`/`AF_INET`/
  `AF_INET6`, otherwise `EAFNOSUPPORT`
- `personality` → only `0` or `0xffffffff`
- a deny list (46 syscalls: `ptrace`, `mount`, `kexec_load`, `bpf`, `perf_event_open`,
  `init_module`, `keyctl`, `add_key`, `request_key`, …) → `EPERM`
- `clone3` → `ENOSYS`, so libc falls back to `clone` and the filter applies
- everything else → `ALLOW`

| | audio | video | artwork | fetcher |
|---|---|---|---|---|
| Program | `mpv` | `mpv` | `ffmpeg`, `ffprobe`, the RSS parser | `yt-dlp` |
| Network | guarded proxy socket only | guarded proxy socket only | none | **host network** (see 8a) |
| Files of yours | none | none | the one file read, the one folder written | the one download folder |
| `/dev/dri` | no | **yes, ro** | no | no |
| Wayland socket | no | **yes** | no | no |
| Memory | 1.5 GiB | 2 GiB | 512 MiB | 2 GiB |
| Tasks | 256 | 256 | 32 | 128 |
| `/tmp` | 16 MiB tmpfs | 16 MiB tmpfs | 16 MiB tmpfs | 16 MiB tmpfs |

Limits for the one-shot helpers are applied with `prlimit` inside the sandbox
rather than on bwrap: a task limit on bwrap itself stops it creating its
namespaces.

The daemon script is copied **into** the sandbox as a `memfd` (`--file`), not
bind-mounted, so no host path is visible from inside.

### 3.1 Fail-closed self-check

Before mpv starts, the sandboxed process verifies its own isolation by reading
`/proc/self/status` and asserting zero `CapEff`/`CapPrm`/`CapAmb`/`CapBnd`,
confirming `/home` does not exist, attempting `connect()` to `1.1.1.1:443` and
`2606:4700:4700::1111:443` and requiring a network-unreachable errno, and
checking that an `AF_VSOCK` socket fails with `EAFNOSUPPORT` (proving the family
filter is live). **Any failure exits 4 and the player never starts.**

If `bwrap` is missing, nothing that would run sandboxed runs at all: not the
player, and not the helpers either - ffprobe and ffmpeg on library files and
artwork, the podcast feed parser, or yt-dlp for search, resolving and
downloads. There is no unsandboxed fallback. A library scan stops before the
index is touched, artwork that cannot be checked is not shown, and the panel
says that bubblewrap is missing and where to install it (Settings › Health).
`NoUnsandboxedFallback` in `tests/test_features.py` runs each of these paths
with bwrap hidden and fails if any of them starts a process.

## 4. The guarded network path

Two reasons the player cannot just be told "you may use the network": mpv
resolves DNS, follows redirects, and expands `.m3u`/HLS playlists itself, so
every redirect and every segment must be re-checked; and the video profile has
no network at all.

### 4.1 Address policy

`public_ip()` accepts only globally-routable unicast. For IPv6 it explicitly
resolves the embedded IPv4 in mapped, 6to4, Teredo, NAT64 and IPv4-compatible
forms, and requires that to be public too — otherwise a `64:ff9b::` address on
an IPv6-only network becomes a tunnel to the user's LAN.

`routes_to_this_machine()` shells out to `ip -j route get <addr>` (cached 60 s)
and requires the route to leave through a **gateway on a default-route device**.
`local`, `broadcast`, `multicast`, `lo` and non-default devices are refused. It
fails closed on any error. A point-to-point default route with no gateway (a
full-tunnel VPN, PPP) is allowed, because that genuinely leaves the machine.

`public_connection(ports)` enforces a port allow-list, rejects the WHATWG
"bad ports" set, resolves through `resolve_public()`, and then calls
`sock.connect()` **on the exact vetted address** — closing the DNS-rebinding
window that a re-resolve would otherwise open.

Two openers exist and nothing else: `API_OPENER` (HTTPS only, redirects
re-checked) for directories and playlists, and `ART_OPENER` (ports 80/443) for
the far less trustworthy artwork hosts. Neither has a `ProxyHandler`, a file
handler, or an FTP handler.

### 4.2 Deadlines

`Deadline` is a context manager plus a watchdog timer that `shutdown()`s every
tracked socket — which also unblocks a read stuck mid-TLS. `bounded_read()`
re-checks the size *after* the read, because a watchdog shutdown looks exactly
like a clean EOF. DNS runs in a side thread joined against the remaining
budget, so a hanging resolver is bounded too. Budgets: 20 s per mirror, 45 s
across mirrors, 15 s per artwork, 10 s pre-flight, 90 s proxy idle.

## 5. Artwork

Artwork is the most obviously hostile input: an arbitrary image, from an
arbitrary host, with an arbitrary content type.

1. Downloaded with a 768 KiB cap, a 15 s deadline, and the `ART_OPENER` port
   allow-list.
2. The **magic bytes** decide the ffmpeg demuxer. There is no probing: a
   PNG-magic-wrapped JPEG is refused, not decoded.
3. Re-encoded in the artwork sandbox with `-max_pixels`, one thread, a 10 s CPU
   rlimit, to a ≤160×160 RGBA PNG.
4. The daemon then **re-parses ffmpeg's PNG strictly** — signature, per-chunk
   CRC32, IHDR must be 8-bit RGBA non-interlaced within the size cap, no
   unknown critical chunks, exact decompressed length, filter bytes ≤ 4, bounded
   `zlib` inflation — and writes a **new minimal PNG built from the pixel
   data**. ffmpeg's byte stream never reaches Qt.
5. Cached by `{uid}-{sha1(url)[:16]}.png`, pruned to 75 % of both a file-count
   and a byte ceiling.

With no artwork, the shell renders initials on a theme-derived tile. No image
from the network is ever decoded by the shell.

## 6. String handling

Every string that came from a source passes through one sanitiser before it
reaches QML:

- non-printable, control and **bidi-override** code points → space
- whitespace collapsed, length-capped per field (title 512, artist 256, …)
- `ensure_ascii=True` on the way out, so a lone surrogate cannot break a write

In QML, every `Text` that renders remote data is `textFormat: Text.PlainText`.
Never `RichText`, never `Text.MarkdownText` — that is a remote-styled-label
primitive, and the mitigation is one property.

## 7. The shell is not a sandbox

Omarchy runs third-party plugin QML **unsandboxed** inside `omarchy-shell`.
That is a property of the host, stated in its own README, and it applies to
Hertz and to us equally. We do not make it worse: the QML performs no network
access, opens no files, and spawns exactly one child — `ap-ctl daemon` — with
a fixed argv. Anything that would make plugin code a liability is done in the
daemon instead.

## 8. Cookies

Opt-in only. Stored `0600` under `~/.config/aurora-pulse/`, read only by the
daemon, sent only through `API_OPENER`, and never to a host outside the guard.
The UI states plainly what cookies are for and never asks for them on startup.
Without them the plugin is fully functional for public content.

## 8a. Helpers, yt-dlp and casting

**One-shot helpers.** ffprobe (tags, lengths), ffmpeg (thumbnails) and the
podcast RSS parser run in the `artwork` profile: no network, and nothing of
yours but the single file they read (bound read-only at its own path) and, for
a thumbnail, the art cache they write. Memory, core size and CPU time are
capped with `prlimit` inside the sandbox. The RSS parser is the plugin's own
code, handed in through the sealed memfd; feed bytes go in on stdin and plain
JSON comes out.

**yt-dlp** runs in the `fetcher` profile. It needs the network directly - it
speaks to YouTube's APIs, which a stream proxy cannot vet - so it shares the
host network namespace and is *not* behind the network guard. It sees no files
but DNS configuration and, when downloading, one private staging folder. That
is the residual risk: a compromised yt-dlp could reach your local network. It
could not read or write anything of yours outside that folder.

**Downloads** land in the staging folder and are moved into Music or Videos
under a name nothing else uses, so a download cannot overwrite, or delete as
an intermediate, a file it did not create. The `FILE` path yt-dlp prints is a
request, not a fact: before anything moves, it must name a regular file - not
a symlink, directory or device, checked with `lstat` - directly inside that
job's staging folder, and the same holds for a cover saved beside it. Anything
else fails the job with nothing moved. yt-dlp and its sandbox have exited by
then, so the file cannot be swapped between the check and the move.

**Recordings** are written by the player into one bound folder
(`Music/AuroraPulse/Recordings/.recording`) with at least 1 GB free required,
and are stopped when free space drops under 300 MB. The player keeps running
while a recording is collected, so its file is measured with `lstat`, moved
only if it is a regular file, and made readable through a descriptor opened
with `O_NOFOLLOW`: a symlink swapped in at the last moment is refused and
removed, never followed to change the permissions of what it points at.
`SandboxOutputContainment` in `tests/test_features.py` covers both.

**Casting** is the only part that talks to private addresses. It sends an SSDP
search (multicast, and directly to each address of the local subnet, since the
firewall drops answers to the multicast one) and asks avahi-daemon for
Chromecasts; it then only ever contacts addresses that answered, and only
private or link-local ones. The Cast protocol runs over TLS without
certificate checks - devices use certificates no browser trusts - so the trust
is in the local address that answered; redirects are not followed and a device's control URLs
must be on the device's own address. A local file is served by a one-file HTTP
server bound to the interface that reaches the renderer, at one random
path, answering only the renderer's address, and shut down when casting stops.

**Installing dependencies** from Settings › Health opens a terminal running
`omarchy-pkg-add` (or `sudo pacman -S --needed`) for package names taken only
from a fixed table in `core/doctor.py`; nothing in a request can add a
package or a word to that command, and nothing installs without your password.

**Subtitles** from YouTube are fetched by the daemon through the network
guard and copied into the player's runtime folder, so the player never fetches
them itself.

## 9. What is *not* defended against

- A malicious **local** user account — the daemon runs as you.
- A compromised **kernel, bubblewrap, or mpv**.
- A hostile **YouTube account** whose video the user explicitly chooses to
  play: that content reaches mpv, which is why mpv is sandboxed.
- **`/dev/dri` in the video profile** is a genuine residual risk. GPU DMA
  requires kernel driver bugs, but they exist. The video player reaches the
  internet only through the guarded proxy (TV is a live HLS stream, so there
  is nothing to pre-resolve), which refuses every private, loopback and
  link-local address.
- **The Wayland socket** in the video profile lets mpv draw, and in principle
  listen, to the compositor. It cannot read your files (there are none), and
  its window is placed by a Hyprland rule matched on its own app id.
- **yt-dlp's network** (see 8a): not behind the guard.

## 10. Tests

The claims above are executable checks in `tests/`:

| File | Asserts |
|---|---|
| `test_units.py` | the network guard refuses private, loopback, link-local, mapped and tunnelled addresses and bad ports; sandbox flags; sanitising; atomic, symlink-proof state; PID reuse |
| `test_regressions.py` | each fixed bug stays fixed |
| `test_features.py` | the sandboxed tool runner reads and writes only what it is given; casting refuses public addresses, and its file server refuses other clients and wrong paths; feed parsing refuses `file://` enclosures and entity bombs |

`./ap-ctl selftest` checks the real sandbox on this machine, including the
isolation self-check run from inside one. Run every suite with
`for t in tests/test_*.py; do python3 $t; done`.
