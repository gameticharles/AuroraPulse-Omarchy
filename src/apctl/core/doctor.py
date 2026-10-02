"""Health checks: is everything this plugin leans on present and working?

AuroraPulse stands on a dozen outside programs and libraries. When one is
missing, the symptom shows up far from the cause - a video that plays as
audio, a download that fails with a cryptic yt-dlp error, media keys that do
nothing - so these are checked by name and reported in plain words, with what
to do about each. The Settings page shows the same list `ap-ctl selftest`
prints.

Every check is quick (no network) so the page can run them on open.
"""

import datetime
import os
import re
import shutil
import subprocess

# name, why it matters, required
BINARIES = (
    ("mpv", "plays everything", True),
    ("pw-cat", "sends the sound to PipeWire", True),
    ("bwrap", "keeps the player and the parsers sandboxed", True),
    ("ffprobe", "reads tags and lengths of your files", True),
    ("ffmpeg", "thumbnails, recording and download conversion", True),
    ("yt-dlp", "YouTube, YouTube Music and downloads", False),
    ("hyprctl", "places the video window", False),
    ("pactl", "plays on another output, like a Bluetooth speaker", False),
    ("wpctl", "remembers the volume for each output", False),
    ("bluetoothctl", "connects paired Bluetooth speakers and headphones", False),
    ("avahi-browse", "finds Chromecasts and Google TVs to cast to", False),
    ("xdg-user-dir", "finds your Music and Videos folders", False),
    ("prlimit", "caps the memory of sandboxed helpers", False),
    ("notify-send", "track-change notifications", False),
)

# The Arch package that provides each one. Only these names can ever be
# handed to the installer, whatever a request asks for.
PACKAGES = {
    "mpv": "mpv", "pw-cat": "pipewire", "bwrap": "bubblewrap", "ffprobe": "ffmpeg",
    "ffmpeg": "ffmpeg", "yt-dlp": "yt-dlp", "pactl": "libpulse", "wpctl": "wireplumber",
    "bluetoothctl": "bluez-utils", "avahi-browse": "avahi",
    "xdg-user-dir": "xdg-user-dirs", "prlimit": "util-linux", "notify-send": "libnotify",
    "PyGObject (MPRIS)": "python-gobject",
}
KNOWN_PACKAGES = frozenset(PACKAGES.values())

# yt-dlp has to keep up with YouTube, which changes something most months.
YTDLP_STALE_DAYS = 60

FIX = {name: "sudo pacman -S %s" % package for name, package in PACKAGES.items()}
FIX["hyprctl"] = "part of Hyprland"


def _check(name, ok, detail, required=True, fix="", level=None, action=""):
    package = PACKAGES.get(name, "") if not ok else ""
    return {"name": name, "ok": bool(ok), "detail": detail, "required": required,
            "fix": "" if ok else fix,
            "level": level or ("ok" if ok else ("error" if required else "warn")),
            # What the Install / Update button does, when there is one.
            "package": package if (package or action) else "",
            "action": action or ("install" if package else "")}


def _run(argv, timeout=5):
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return out.returncode, (out.stdout or "") + (out.stderr or "")
    except (OSError, subprocess.SubprocessError):
        return -1, ""


def ytdlp_age(version):
    """Days since a yt-dlp release, from its date-shaped version, or None."""
    match = re.match(r"(\d{4})\.(\d{1,2})\.(\d{1,2})", str(version or "").strip())
    if not match:
        return None
    try:
        released = datetime.date(int(match.group(1)), int(match.group(2)),
                                 int(match.group(3)))
    except ValueError:
        return None
    return (datetime.date.today() - released).days


def binaries():
    out = []
    for name, why, required in BINARIES:
        path = shutil.which(name) or (
            "/usr/bin/" + name if os.path.exists("/usr/bin/" + name) else "")
        out.append(_check(name, bool(path), why if path else "missing: " + why,
                          required, FIX.get(name, "")))
    return out


def ytdlp():
    from . import resolver
    binary = shutil.which(resolver.YTDLP) or (
        resolver.YTDLP if os.path.exists(str(resolver.YTDLP)) else "")
    if not binary:
        return None
    code, text = _run([binary, "--version"])
    version = text.strip().splitlines()[0] if code == 0 and text.strip() else ""
    age = ytdlp_age(version)
    if age is None:
        return _check("yt-dlp version", bool(version), version or "could not be read",
                      False, "reinstall yt-dlp")
    fresh = age <= YTDLP_STALE_DAYS
    check = _check("yt-dlp version", fresh,
                   "%s (%d days old)" % (version, age) if not fresh else version,
                   False, "YouTube changes often; update it: sudo pacman -Syu yt-dlp",
                   level="ok" if fresh else "warn", action="" if fresh else "update")
    if not fresh:
        check["package"] = "yt-dlp"
    return check


def python_modules():
    try:
        import gi
        gi.require_version("Gio", "2.0")
        from gi.repository import Gio  # noqa: F401
        ok, detail = True, "media keys, playerctl and the lock screen"
    except (ImportError, ValueError) as exc:
        ok, detail = False, "media keys will not work (%s)" % str(exc)[:60]
    return _check("PyGObject (MPRIS)", ok, detail, False, "sudo pacman -S python-gobject")


def folders():
    from .downloads import audio_dir, video_dir
    out = []
    for label, path in (("Music folder", os.path.dirname(audio_dir())),
                        ("Videos folder", os.path.dirname(video_dir()))):
        exists = os.path.isdir(path)
        writable = exists and os.access(path, os.W_OK)
        out.append(_check(label, writable, path if writable else
                          ("%s is not writable" % path if exists else "%s does not exist" % path),
                          False, "mkdir -p %s" % path))
    try:
        free = shutil.disk_usage(os.path.dirname(audio_dir())).free
        out.append(_check("Free space", free >= 1 << 30, "%.1f GB" % (free / (1 << 30)),
                          False, "downloads and recordings need room"))
    except OSError:
        pass
    return out


def sandbox_works():
    from ..util import sandbox
    try:
        cmd, fds = sandbox.sandbox_command("artwork")
    except sandbox.SandboxUnavailable as exc:
        return _check("Sandbox", False, str(exc)[:100], True,
                      "check that unprivileged user namespaces are enabled")
    try:
        proc = subprocess.run(cmd + ["--", "/usr/bin/true"], capture_output=True,
                              pass_fds=fds, timeout=10)
        ok = proc.returncode == 0
        detail = "helpers run isolated" if ok else (
            proc.stderr.decode("utf-8", "replace").strip()[:100] or "exit %d" % proc.returncode)
    except (OSError, subprocess.SubprocessError) as exc:
        ok, detail = False, str(exc)[:100]
    finally:
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass
    return _check("Sandbox", ok, detail, True,
                  "check that unprivileged user namespaces are enabled")


def databases(store):
    out = []
    try:
        library = store.library
        tracks = library.count_tracks()
        out.append(_check("Library database", True, "%d files indexed" % tracks))
    except Exception as exc:  # noqa: BLE001
        out.append(_check("Library database", False, str(exc)[:100], True,
                          "it is rebuilt by a rescan; favourites may be lost"))
    return out


def run_all(store=None):
    checks = binaries()
    extra = ytdlp()
    if extra:
        checks.append(extra)
    checks.append(python_modules())
    checks.append(sandbox_works())
    checks += folders()
    if store is not None:
        checks += databases(store)
    return checks


def summary(checks):
    errors = sum(1 for c in checks if c["level"] == "error")
    warnings = sum(1 for c in checks if c["level"] == "warn")
    if errors:
        return "error", "%d problem%s need fixing" % (errors, "" if errors == 1 else "s")
    if warnings:
        return "warn", "%d warning%s" % (warnings, "" if warnings == 1 else "s")
    return "ok", "Everything is working"


# -- installing what is missing ---------------------------------------------------

_PACKAGE_NAME = re.compile(r"^[a-z0-9][a-z0-9+._-]{0,60}$")


def installable(checks):
    """The packages that would fix the failing checks: (installs, updates)."""
    installs, updates = [], []
    for check in checks:
        if check.get("ok") or not check.get("package"):
            continue
        target = updates if check.get("action") == "update" else installs
        if check["package"] not in target:
            target.append(check["package"])
    return installs, updates


def install_command(packages, update=False):
    """argv that opens a terminal and installs (or updates) `packages`.

    A terminal, because pacman needs your password and should show you what
    it is about to do; nothing is installed silently. Only package names from
    PACKAGES are accepted.
    """
    import shlex
    wanted = [p for p in packages if p in KNOWN_PACKAGES and _PACKAGE_NAME.match(p)]
    if not wanted:
        return None
    names = " ".join(shlex.quote(p) for p in wanted)
    if update:
        # Arch does not do partial upgrades: a newer yt-dlp comes with -Syu.
        inner = "echo 'Updating %s…'; sudo pacman -Syu --needed %s" % (names, names)
    elif shutil.which("omarchy-pkg-add"):
        inner = "echo 'Installing %s…'; omarchy-pkg-add %s" % (names, names)
    else:
        inner = "echo 'Installing %s…'; sudo pacman -S --needed %s" % (names, names)
    if shutil.which("omarchy-launch-floating-terminal-with-presentation"):
        return ["omarchy-launch-floating-terminal-with-presentation", inner]
    terminal = shutil.which("xdg-terminal-exec")
    if terminal:
        return [terminal, "bash", "-c", inner + "; read -rp 'Press Enter to close. '"]
    return None


def is_installed(package):
    code, _text = _run(["pacman", "-Q", package], timeout=5)
    return code == 0


def prompt_names(checks, first_run=False):
    """What the "something is missing" prompt lists: anything required, a
    missing or outdated yt-dlp, and - the first time - everything missing."""
    out = []
    for check in checks:
        if check.get("ok"):
            continue
        if first_run or check.get("required") or check["name"].startswith("yt-dlp"):
            out.append(check["name"])
    return out
